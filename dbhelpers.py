import datetime
from database import *


def get_or_create(session, dtype, **kwargs):
    """Return either an existing object with the given properties, or a new one."""
    obj = session.query(dtype).filter_by(**kwargs).first()
    if obj is None:
        obj = dtype(**kwargs)
        session.add(obj)

    return obj


def add_ids_to_list(session, listing_ids, list_name):
    """Add all listings specified in ids to list named list_name. Will create a new list if necessary. """
    add_list = get_or_create(session, List, name=list_name)

    for listing_id in listing_ids:
        get_or_create(session, ListMembership, list=add_list, listing_id=listing_id)

    first = session.query(Listing).filter_by(id=listing_ids[0]).first()
    add_list.is_amazon = isinstance(first, AmazonListing)


def remove_ids_from_list(session, listing_ids, list_name):
    """Remove all specified listings from the given list."""
    rm_list = session.query(List).filter_by(name=list_name).first()
    if not rm_list or not listing_ids:
        return

    for listing_id in listing_ids:
        membership = session.query(ListMembership).filter_by(list_id=rm_list.id, listing_id=listing_id).first()
        if membership:
            session.delete(membership)


def link_products_ids(session, amz_listing_id, vnd_listing_id):
    """Retrieve or create a link between two products, based on their IDs."""
    link = get_or_create(session, LinkedProducts, amz_listing_id=amz_listing_id, vnd_listing_id=vnd_listing_id)
    if link.confidence is None:
        session.flush()
        link.build_confidence()

    return link


def link_products(session, amz, vnd):
    """Retrieve or create a link between two products."""
    link = get_or_create(session, LinkedProducts, amz_listing_id=amz.id, vnd_listing_id=vnd.id)
    if link.confidence is None:
        link.amz_listing = amz
        link.vnd_listing = vnd
        link.build_confidence()

    return link


def unlink_products(session, amz_listing_id, vnd_listing_id):
    """Delete a link between two products, if one exists."""
    link = session.query(LinkedProducts).filter_by(amz_listing_id=amz_listing_id, vnd_listing_id=vnd_listing_id).first()
    if link:
        session.delete(link)


def get_watch(session, listing_id):
    """Return the repeating UpdateAmazonListing operation associated with this listing, or none."""
    watch = session.query(Operation).filter_by(listing_id=listing_id).\
                                     filter(and_(Operation.operation == 'UpdateAmazonListing',
                                                 Operation.param_string.contains('repeat'))).\
                                     first()
    return watch


def set_watch(session, listing_id, time):
    """Create or modify a product watch, or delete if time=None."""
    watch = get_watch(session, listing_id)

    if time is None:
        if watch:
            session.delete(watch)
    else:
        watch = watch or Operation.UpdateAmazonListing(listing_id=listing_id, priority=5)
        watch.params = {'log': True, 'repeat': time}
        watch.scheduled = func.now()
        session.add(watch)


def get_or_create_category(session, productcategory_id, product_group):
    """Get or create an Amazon product category, given a ProductCategoryId or ProductGroup value. If both values are
    None, returns the 'Unknown' category.
    """
    category = None

    if productcategory_id:
        category = get_or_create(session, AmazonCategory, product_category_id=productcategory_id)
        category.name = category.name or productcategory_id
    elif product_group:
        category = session.query(AmazonCategory).filter(AmazonCategory.product_groups.contains(product_group)).first()

    return category or get_or_create(session, AmazonCategory, name='Unknown')


class ProductHistoryStats:
    """A helper class for working with Amazon product history data."""

    _sale_slope = -0.3

    def __init__(self, session, amz_listing_id):
        """Initialize the object."""

        self._session = session
        self._listing_id = amz_listing_id

    def data_points(self):
        """Return historical data in a list of named tuples."""
        return self._session.query(AmzProductHistory.timestamp,
                                   AmzProductHistory.salesrank,
                                   AmzProductHistory.hasprime,
                                   AmzProductHistory.merchant_id,
                                   AmzProductHistory.offers).\
                             filter_by(amz_listing_id=self._listing_id).\
                             order_by(AmzProductHistory.timestamp.desc()).\
                             all()

    def avg_salesrank(self):
        """Return the listing's average sales rank."""
        return self._session.query(func.avg(AmzProductHistory.salesrank)).\
                             filter_by(amz_listing_id=self._listing_id).\
                             scalar()

    def avg_90day_salesrank(self):
        """Return the listing's average sales rank over the last 90 days."""
        start_date = datetime.date.today() - datetime.timedelta(days=90)

        return self._session.query(func.avg(AmzProductHistory.salesrank)).\
                             filter(AmzProductHistory.amz_listing_id==self._listing_id,
                                    AmzProductHistory.timestamp > start_date).\
                             scalar()

    def sales_points(self):
        """Return the timestamps of data points signifying a sale."""
        sales = []
        last_point = None

        for point in self._session.query(AmzProductHistory.timestamp, AmzProductHistory.salesrank).\
                                   filter_by(amz_listing_id=self._listing_id).\
                                   order_by(AmzProductHistory.timestamp.\
                                   asc()):

            if last_point:
                slope = (point.salesrank - last_point.salesrank) \
                        / (point.timestamp.timestamp() - last_point.timestamp.timestamp())

                if slope < self._sale_slope:
                    sales.append(point)

            last_point = point

        return sales

    def is_sale(self, obs):
        """Return True if the given AmzProductHistory object is believed to show a sale."""

        # Get the observation immediately before this one
        prior = self._session.query(AmzProductHistory).\
                              filter(AmzProductHistory.amz_listing_id==obs.amz_listing_id,
                                     AmzProductHistory.id < obs.id).\
                              order_by(AmzProductHistory.timestamp.desc()).\
                              first()

        if prior:
            # Calculate how much of a drop between prior.salesrank and obs.salesrank
            try:
                slope = (obs.salesrank - prior.salesrank) / (obs.timestamp.timestamp() - prior.timestamp.timestamp())
                return slope < self._sale_slope
            except ZeroDivisionError:
                print('Zero division error: obs.id=%s, prior.id=%s' % (obs.id, prior.id))
                return False
        else:
            return False
