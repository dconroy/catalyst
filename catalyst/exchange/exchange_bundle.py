import calendar
import os
import time
from datetime import timedelta, datetime, date

import pandas as pd
from logbook import Logger, INFO

from catalyst import get_calendar
from catalyst.data.minute_bars import BcolzMinuteOverlappingData, \
    BcolzMinuteBarWriter, BcolzMinuteBarReader, BcolzMinuteBarMetadata
from catalyst.data.us_equity_pricing import BcolzDailyBarWriter, \
    BcolzDailyBarReader
from catalyst.exchange.bundle_utils import get_ffill_candles, get_start_dt, \
    get_periods, range_in_bundle, get_bcolz_chunk
from catalyst.exchange.exchange_utils import get_exchange_folder, \
    get_exchange_bundles_folder
from catalyst.utils.cli import maybe_show_progress
from catalyst.utils.deprecate import deprecated
from catalyst.utils.paths import ensure_directory


def _cachpath(symbol, type_):
    return '-'.join([symbol, type_])


BUNDLE_NAME_TEMPLATE = '{root}/{frequency}_bundle'
log = Logger('exchange_bundle')
log.level = INFO


class ExchangeBundle:
    def __init__(self, exchange):
        self.exchange = exchange
        self.minutes_per_day = 1440
        self.default_ohlc_ratio = 1000000
        self._writers = dict()
        self._readers = dict()
        self.calendar = get_calendar('OPEN')

    def get_assets(self, include_symbols, exclude_symbols):
        # TODO: filter exclude symbols assets
        if include_symbols is not None:
            include_symbols_list = include_symbols.split(',')

            return self.exchange.get_assets(include_symbols_list)

        else:
            return self.exchange.get_assets()

    def get_adj_dates(self, start, end, assets):
        now = pd.Timestamp.utcnow()
        if end is None or end > now:
            log.debug('adjusting the end date to now {}'.format(now))
            end = now

        earliest_trade = None
        for asset in assets:
            if earliest_trade is None or earliest_trade > asset.start_date:
                earliest_trade = asset.start_date

        if start is None or earliest_trade > start:
            log.debug(
                'adjusting start date to earliest trade date found {}'.format(
                    earliest_trade
                ))
            start = earliest_trade

        if start >= end:
            raise ValueError('start date cannot be after end date')

        return start, end

    def get_reader(self, data_frequency):
        """
        Get a data writer object, either a new object or from cache

        :return: BcolzMinuteBarReader or BcolzDailyBarReader
        """
        if data_frequency in self._readers \
                and self._readers[data_frequency] is not None:
            return self._readers[data_frequency]

        root = get_exchange_folder(self.exchange.name)
        input_dir = BUNDLE_NAME_TEMPLATE.format(
            root=root,
            frequency=data_frequency
        )

        self._readers[data_frequency] = None
        if data_frequency == 'minute':
            try:
                self._readers[data_frequency] = BcolzMinuteBarReader(input_dir)
            except IOError:
                log.debug('no reader data found in {}'.format(input_dir))

        elif data_frequency == 'daily':
            try:
                self._readers[data_frequency] = BcolzDailyBarReader(input_dir)
            except IOError:
                log.debug('no reader data found in {}'.format(input_dir))
        else:
            raise ValueError(
                'invalid frequency {}'.format(data_frequency)
            )

        return self._readers[data_frequency]

    def update_metadata(self, writer, start_dt, end_dt):
        pass

    def get_writer(self, start_dt, end_dt, data_frequency):
        """
        Get a data writer object, either a new object or from cache

        :return: BcolzMinuteBarWriter or BcolzDailyBarWriter
        """
        key = data_frequency
        if key in self._writers:
            return self._writers[key]

        root = get_exchange_folder(self.exchange.name)
        output_dir = BUNDLE_NAME_TEMPLATE.format(
            root=root,
            frequency=data_frequency
        )
        ensure_directory(output_dir)

        if data_frequency == 'minute':
            if len(os.listdir(output_dir)) > 0:

                metadata = BcolzMinuteBarMetadata.read(output_dir)

                write_metadata = False
                if start_dt < metadata.start_session:
                    write_metadata = True
                    start_session = start_dt
                else:
                    start_session = metadata.start_session

                if end_dt > metadata.end_session:
                    write_metadata = True

                    end_session = end_dt
                else:
                    end_session = metadata.end_session

                self._writers[key] = \
                    BcolzMinuteBarWriter(
                        output_dir,
                        metadata.calendar,
                        start_session,
                        end_session,
                        metadata.minutes_per_day,
                        metadata.default_ohlc_ratio,
                        metadata.ohlc_ratios_per_sid,
                        write_metadata=write_metadata
                    )
            else:
                self._writers[key] = BcolzMinuteBarWriter(
                    rootdir=output_dir,
                    calendar=self.calendar,
                    minutes_per_day=self.minutes_per_day,
                    start_session=start_dt,
                    end_session=end_dt,
                    write_metadata=True,
                    default_ohlc_ratio=self.default_ohlc_ratio
                )

        elif data_frequency == 'daily':
            if len(os.listdir(output_dir)) > 0:
                self._writers[key] = \
                    BcolzDailyBarWriter.open(output_dir, end_dt)
            else:
                end_session = end_dt.floor('1d')
                self._writers[key] = BcolzDailyBarWriter(
                    filename=output_dir,
                    calendar=self.calendar,
                    start_session=start_dt,
                    end_session=end_session
                )
        else:
            raise ValueError(
                'invalid frequency {}'.format(data_frequency)
            )

        return self._writers[key]

    def filter_existing_assets(self, assets, start_dt, end_dt, data_frequency):
        """
        For each asset, get the close on the start and end dates of the chunk.
            If the data exists, the chunk ingestion is complete.
            If any data is missing we ingest the data.

        :param assets: list[TradingPair]
            The assets is scope.
        :param start_dt:
            The chunk start date.
        :param end_dt:
            The chunk end date.
        :return: list[TradingPair]
            The assets missing from the bundle
        """
        reader = self.get_reader(data_frequency)
        missing_assets = []
        for asset in assets:
            has_data = range_in_bundle(asset, start_dt, end_dt, reader)

            if not has_data:
                missing_assets.append(asset)

        return missing_assets

    def _write(self, data, writer, data_frequency):
        """
        Write data to the writer

        :param df:
        :param writer:
        :return:
        """
        try:
            writer.write(
                data=data,
                show_progress=False,
                invalid_data_behavior='raise'
            )
        except BcolzMinuteOverlappingData as e:
            log.warn('chunk already exists: {}'.format(e))
        except Exception as e:
            log.warn('error when writing data: {}, trying again'.format(e))

            # This is workaround, there is an issue with empty
            # session_label when using a newly created writer
            del self._writers[data_frequency]

            # TODO: these are the dates of the chunk, not the job
            writer = self.get_writer(writer._start_session,
                                     writer._end_session, data_frequency)
            writer.write(
                data=data,
                show_progress=False,
                invalid_data_behavior='raise'
            )

    def ingest_chunk(self, bar_count, end_dt, data_frequency, asset,
                     writer, previous_candle=dict()):
        """
        Retrieve the specified OHLCV chunk and write it to the bundle

        :param bar_count:
        :param end_dt:
        :param data_frequency:
        :param asset:
        :param writer:
        :param previous_candle
        :return:
        """

        # The get_history method supports multiple asset
        candles = self.exchange.get_history(
            assets=[asset],
            end_dt=end_dt,
            bar_count=bar_count,
            data_frequency=data_frequency,
            fallback_exchange=False
        )

        num_candles = 0
        data = []
        for asset in candles:
            asset_candles = candles[asset]
            if not asset_candles:
                log.debug(
                    'no data: {symbols} on {exchange}, date {end}'.format(
                        symbols=asset,
                        exchange=self.exchange.name,
                        end=end_dt
                    )
                )
                continue

            previous = previous_candle[asset] \
                if asset in previous_candle else None

            all_dates, all_candles = get_ffill_candles(
                candles=asset_candles,
                bar_count=bar_count,
                end_dt=end_dt,
                data_frequency=data_frequency,
                previous_candle=previous
            )
            previous_candle[asset] = all_candles[-1]

            df = pd.DataFrame(
                data=all_candles,
                index=all_dates,
                columns=['open', 'high', 'low', 'close', 'volume']
            )

            if not df.empty:
                df.sort_index(inplace=True)

                sid = asset.sid
                num_candles += len(df.values)

                data.append((sid, df))

        log.debug(
            'writing {num_candles} candles for {bar_count} bars'
            'ending {end}'.format(
                num_candles=num_candles,
                bar_count=bar_count,
                end=end_dt
            )
        )
        self._write(data, writer, data_frequency)

        return data

    def download_bundle(self, name):
        """

        :param name:
        :return:
        """

    def ingest_ctable(self, asset, data_frequency, period, writer):
        start_time = time.time()

        path = get_bcolz_chunk(
            exchange_name=self.exchange.name,
            symbol=asset.symbol,
            data_frequency=data_frequency,
            period=period
        )

        reader = BcolzMinuteBarReader(path)

        start = reader.first_trading_day
        end = reader.last_available_dt

        periods = self.calendar.minutes_in_range(start, end)

        sid = asset.sid
        arrays = reader.load_raw_arrays(
            fields=['open', 'high', 'low', 'close', 'volume'],
            start_dt=start,
            end_dt=end,
            sids=[sid]
        )

        ohlcv = dict(
            open=arrays[0].flatten(),
            high=arrays[1].flatten(),
            low=arrays[2].flatten(),
            close=arrays[3].flatten(),
            volume=arrays[4].flatten()
        )

        df = pd.DataFrame(
            data=ohlcv,
            index=periods
        )

        data = []
        if not df.empty:
            df.sort_index(inplace=True, ascending=False)

            data.append((sid, df))

        self._write(data, writer, data_frequency)

        end_time = time.time()
        delta_time = end_time - start_time

        log.info('time elapsed {}'.format(delta_time))

        pass

    def ingest(self, data_frequency, include_symbols=None,
               exclude_symbols=None, start=None, end=None,
               show_progress=True, environ=os.environ):
        """

        :param data_frequency:
        :param include_symbols:
        :param exclude_symbols:
        :param start:
        :param end:
        :param show_progress:
        :param environ:
        :return:
        """

        assets = self.get_assets(include_symbols, exclude_symbols)
        start, end = self.get_adj_dates(start, end, assets)
        reader = self.get_reader(data_frequency)

        chunks = []
        periods = []
        for asset in assets:
            asset_start, asset_end = self.get_adj_dates(start, end, [asset])
            sessions = self.calendar.sessions_in_range(asset_start, asset_end)

            dt = sessions[0]
            while dt <= sessions[-1]:
                period = '{}-{}'.format(dt.year, dt.month)

                if period not in periods:
                    periods.append(period)

                    month_range = calendar.monthrange(dt.year, dt.month)
                    month_start = date(dt.year, dt.month, month_range[0])
                    month_end = date(dt.year, dt.month, month_range[1])

                    if not range_in_bundle(asset, month_start, month_end,
                                           reader):
                        log.debug('adding period: {}'.format(period))
                        chunks.append(
                            dict(
                                asset=asset,
                                period_end=month_end,
                                period=period
                            )
                        )

                dt += timedelta(days=1)

        chunks.sort(key=lambda chunk: chunk['period_end'])

        writer = self.get_writer(start, end, data_frequency)
        with maybe_show_progress(
                chunks,
                show_progress,
                label='Fetching {exchange} {frequency} candles: '.format(
                    exchange=self.exchange.name,
                    frequency=data_frequency
                )) as it:

            for chunk in it:
                self.ingest_ctable(
                    asset=chunk['asset'],
                    data_frequency=data_frequency,
                    period=chunk['period'],
                    writer=writer
                )
        pass
