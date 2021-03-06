from logbook import Logger

log = Logger('AssetFinderExchange')


class AssetFinderExchange(object):
    def __init__(self, exchange):
        self.exchange = exchange
        self._asset_cache = {}

    @property
    def sids(self):
        """
        This seems to be used to pre-fetch assets.
        I don't think that we need this for live-trading.
        Leaving the list empty.
        """
        return list()

    def retrieve_all(self, sids, default_none=False):
        """
        Retrieve all assets in `sids`.

        Parameters
        ----------
        sids : iterable of int
            Assets to retrieve.
        default_none : bool
            If True, return None for failed lookups.
            If False, raise `SidsNotFound`.

        Returns
        -------
        assets : list[Asset or None]
            A list of the same length as `sids` containing Assets (or Nones)
            corresponding to the requested sids.

        Raises
        ------
        SidsNotFound
            When a requested sid is not found and default_none=False.
        """
        for sid in sids:
            if sid in self._asset_cache:
                log.info('got asset from cache: {}'.format(sid))
            else:
                log.info('fetching asset: {}'.format(sid))
        return list()

    def lookup_symbol(self, symbol, as_of_date, fuzzy=False):
        """Lookup an asset by symbol.

        Parameters
        ----------
        symbol : str
            The ticker symbol to resolve.
        as_of_date : datetime or None
            Look up the last owner of this symbol as of this datetime.
            If ``as_of_date`` is None, then this can only resolve the equity
            if exactly one equity has ever owned the ticker.
        fuzzy : bool, optional
            Should fuzzy symbol matching be used? Fuzzy symbol matching
            attempts to resolve differences in representations for
            shareclasses. For example, some people may represent the ``A``
            shareclass of ``BRK`` as ``BRK.A``, where others could write
            ``BRK_A``.

        Returns
        -------
        equity : Asset
            The equity that held ``symbol`` on the given ``as_of_date``, or the
            only equity to hold ``symbol`` if ``as_of_date`` is None.

        Raises
        ------
        SymbolNotFound
            Raised when no equity has ever held the given symbol.
        MultipleSymbolsFound
            Raised when no ``as_of_date`` is given and more than one equity
            has held ``symbol``. This is also raised when ``fuzzy=True`` and
            there are multiple candidates for the given ``symbol`` on the
            ``as_of_date``.
        """
        log.debug('looking up symbol: {}'.format(symbol))

        if symbol in self._asset_cache:
            return self._asset_cache[symbol]
        else:
            asset = self.exchange.get_asset(symbol)
            self._asset_cache[symbol] = asset
            return asset
