from abc import abstractmethod
from collections import defaultdict
import numpy as np
import pandas as pd
from six import viewvalues
from toolz import groupby
from zipline.lib.adjusted_array import AdjustedArray
from zipline.lib.adjustment import (Datetime641DArrayOverwrite,
                                    Float641DArrayOverwrite)

from zipline.pipeline.common import (
    EVENT_DATE_FIELD_NAME,
    FISCAL_QUARTER_FIELD_NAME,
    FISCAL_YEAR_FIELD_NAME,
    SID_FIELD_NAME,
    TS_FIELD_NAME,
)
from zipline.pipeline.loaders.base import PipelineLoader
from zipline.utils.numpy_utils import datetime64ns_dtype
from zipline.pipeline.loaders.utils import (
    ffill_across_cols,
    last_in_date_group
)

NORMALIZED_QUARTERS = 'normalized_quarters'

SHIFTED_NORMALIZED_QTRS = 'shifted_normalized_quarters'

NEXT_FISCAL_QUARTER = 'next_fiscal_quarter'
NEXT_FISCAL_YEAR = 'next_fiscal_year'
PREVIOUS_FISCAL_QUARTER = 'previous_fiscal_quarter'
PREVIOUS_FISCAL_YEAR = 'previous_fiscal_year'
SIMULTATION_DATES = 'dates'


def normalize_quarters(years, quarters):
    return years * 4 + quarters - 1


def split_normalized_quarters(normalized_quarters):
    years = normalized_quarters // 4
    quarters = normalized_quarters % 4
    return years, quarters + 1


def required_estimates_fields(columns):
    """
    Compute the set of resource columns required to serve
    `columns`.
    """
    # These metadata columns are used to align event indexers.
    return {
        TS_FIELD_NAME,
        SID_FIELD_NAME,
        EVENT_DATE_FIELD_NAME,
        FISCAL_QUARTER_FIELD_NAME,
        FISCAL_YEAR_FIELD_NAME
    }.union(
        # We also expect any of the field names that our loadable columns
        # are mapped to.
        viewvalues(columns),
    )


def validate_column_specs(events, columns):
    """
    Verify that the columns of ``events`` can be used by a
    QuarterEstimatesLoader to serve the BoundColumns described by
    `columns`.
    """
    required = required_estimates_fields(columns)
    received = set(events.columns)
    missing = required - received
    if missing:
        raise ValueError(
            "QuarterEstimatesLoader missing required columns {missing}.\n"
            "Got Columns: {received}\n"
            "Expected Columns: {required}".format(
                missing=sorted(missing),
                received=sorted(received),
                required=sorted(required),
            )
        )


class QuarterEstimatesLoader(PipelineLoader):
    def __init__(self,
                 estimates,
                 base_column_name_map):
        validate_column_specs(
            estimates,
            base_column_name_map
        )

        self.estimates = estimates[
            estimates[EVENT_DATE_FIELD_NAME].notnull() &
            estimates[FISCAL_QUARTER_FIELD_NAME].notnull() &
            estimates[FISCAL_YEAR_FIELD_NAME].notnull()
        ]

        self.base_column_name_map = base_column_name_map

    @abstractmethod
    def load_quarters(self, num_quarters, last, dates):
        pass

    @staticmethod
    def overwrite_with_missing_value(missing_value,
                                     last,
                                     qtr_start_idx,
                                     adjustments,
                                     sid_idx,
                                     overwrite):
        # Overwrite all values before this quarter with the missing value.
        adjustments[qtr_start_idx] = [
            overwrite(0,
                      qtr_start_idx - 1,
                      sid_idx,
                      sid_idx,
                      np.array(
                          [missing_value] *
                          len(last.index[:qtr_start_idx]))
                      )
        ]

    def get_adjustments(self,
                        result,
                        subsequent_qtr_data,
                        col_result,
                        last,
                        column_name,
                        column,
                        mask,
                        assets):
        adjustments = defaultdict(list)
        if column.dtype == datetime64ns_dtype:
            overwrite = Datetime641DArrayOverwrite
            missing_value = pd.NaT
        else:
            overwrite = Float641DArrayOverwrite
            missing_value = np.NaN
        for sid_idx, sid in enumerate(assets):
            sid_result = result[result.index.get_level_values(
                SID_FIELD_NAME
            ) == sid]
            # Determine where we think quarters are changing for this sid.
            qtr_shifts = sid_result[
                sid_result[SHIFTED_NORMALIZED_QTRS] !=
                sid_result[SHIFTED_NORMALIZED_QTRS].shift(1)
            ]
            # For the given sid, determine which quarters we have estimates
            # for.
            quarters_with_estimates_for_sid = last.xs(
                sid, axis=1, level=SID_FIELD_NAME
            ).groupby(axis=1, level=1).first().columns.values
            # Iterate up until the last quarter's event is announced.
            for row_indexer in list(qtr_shifts.index):
                # Find the requested quarter number for this quarter and add
                # 1 to get the next requested quarter.
                requested_quarter = qtr_shifts.loc[
                    row_indexer
                ][SHIFTED_NORMALIZED_QTRS] + 1
                # Find the starting index of the quarter that comes right
                # after this row. This isn't the starting index of the
                # requested quarter, but simply the date we cross over into a
                # new quarter and on which we will begin returning estimates
                # for the requested quarter.
                qtr_start_idx = last.index.searchsorted(
                    subsequent_qtr_data.iloc[
                        result.index.get_loc(row_indexer)
                    ][EVENT_DATE_FIELD_NAME],
                    side='right'  # We always want the index of the
                    # next date after the release.
                )
                # If a quarter was requested, there are estimates for
                # it, and the quarter starts somewhere in our date
                # index, overwrite all values going up to the starting index
                # of that quarter with estimates for that quarter.
                if (requested_quarter in quarters_with_estimates_for_sid and
                        #  Our 'next' quarter can never start at index 0.
                        0 < qtr_start_idx < len(last.index)):
                    adjustments[qtr_start_idx] = \
                        [overwrite(
                            0,
                            qtr_start_idx - 1,  # overwrite thru last qtr
                            sid_idx,
                            sid_idx,
                            last[column_name,
                                 requested_quarter,
                                 sid][:qtr_start_idx].values)]
                # If a quarter was requested that starts within our date index
                # but there are no estimates for it, overwrite all values
                # going up to the starting index of that quarter with the
                # missing value for this column. Our 'next' quarter can never
                # start at index 0. A starting index of 0 means that the next
                # quarter's event date was NaT.
                elif 0 < qtr_start_idx < len(last.index):
                    self.overwrite_with_missing_value(missing_value,
                                                      last,
                                                      qtr_start_idx,
                                                      adjustments,
                                                      sid_idx,
                                                      overwrite)
                # TODO: what is the 'else' case for this?
        return AdjustedArray(
                col_result.values.astype(column.dtype),
                mask,
                dict(adjustments),
                column.missing_value,
            )

    def load_adjusted_array(self, columns, dates, assets, mask):
        # TODO: how can we enforce that datasets have the num_quarters
        # attribute, given that they're created dynamically?
        groups = groupby(lambda x: x.dataset.num_quarters, columns)
        groups_columns = dict(groups)
        if (pd.Series(groups_columns.keys()) < 0).any():
            raise ValueError("Must pass a number of quarters >= 0")
        out = {}
        self.estimates[NORMALIZED_QUARTERS] = normalize_quarters(
            self.estimates[FISCAL_YEAR_FIELD_NAME],
            self.estimates[FISCAL_QUARTER_FIELD_NAME],
        )
        for num_quarters, columns in groups_columns.items():
            # The column's dataset is itself dynamic and the mapping we
            # actually want is to its dataset's parent's column name.
            name_map = {c: self.base_column_name_map[
                            getattr(c.dataset.__base__, c.name)
                        ] for c in columns}
            # Determine the last piece of information we know for each column
            # on each date in the index for each sid and quarter.
            last_per_qtr = last_in_date_group(
                self.estimates, True, dates, assets,
                extra_groupers=[NORMALIZED_QUARTERS]
            )

            # Forward fill values for each quarter.
            ffill_across_cols(last_per_qtr, columns)
            # Stack quarter and sid into the index
            stacked_last_per_qtr = last_per_qtr.stack([NORMALIZED_QUARTERS,
                                                       SID_FIELD_NAME])
            # Set date index name for ease of reference
            stacked_last_per_qtr.index.set_names(SIMULTATION_DATES, 0, True)
            # Determine which quarter is next/previous for each date.
            subsequent_qtr_data = self.load_quarters(num_quarters,
                                                     stacked_last_per_qtr)
            requested_qtr_data = stacked_last_per_qtr.loc[
                subsequent_qtr_data.index
            ]
            if not requested_qtr_data.empty:
                # We no longer need this in the index, but we do need it as a
                # column for adjustments.
                requested_qtr_data = requested_qtr_data.reset_index(
                    SHIFTED_NORMALIZED_QTRS
                )
                (requested_qtr_data[FISCAL_YEAR_FIELD_NAME],
                 requested_qtr_data[FISCAL_QUARTER_FIELD_NAME]) = \
                    split_normalized_quarters(
                        requested_qtr_data[SHIFTED_NORMALIZED_QTRS]
                    )
                for c in columns:
                    column_name = name_map[c]
                    col_result = requested_qtr_data[
                        column_name
                    ].unstack(SID_FIELD_NAME).reindex(dates)
                    adjusted_array = self.get_adjustments(requested_qtr_data,
                                                          subsequent_qtr_data,
                                                          col_result,
                                                          last_per_qtr,
                                                          column_name,
                                                          c,
                                                          mask,
                                                          assets)
                    out[c] = adjusted_array
        return out


class NextQuartersEstimatesLoader(QuarterEstimatesLoader):

    def load_quarters(self, num_quarters, stacked_last_per_qtr):
        # Filter for releases that are on or after each simulation date and
        # determine the next quarter by picking out the upcoming release for
        # each date in the index.
        stacked_last_per_qtr = stacked_last_per_qtr.sort(
            EVENT_DATE_FIELD_NAME
        )
        next_releases_per_date = stacked_last_per_qtr.loc[
            stacked_last_per_qtr[EVENT_DATE_FIELD_NAME] >=
            stacked_last_per_qtr.index.get_level_values(SIMULTATION_DATES)
        ].groupby(level=[SIMULTATION_DATES, SID_FIELD_NAME]).nth(0)
        next_releases_per_date[
            SHIFTED_NORMALIZED_QTRS
        ] = next_releases_per_date.index.get_level_values(
            NORMALIZED_QUARTERS
        ) + (num_quarters - 1)
        next_releases_per_date = next_releases_per_date.set_index([
            next_releases_per_date.index.get_level_values(SIMULTATION_DATES),
            SHIFTED_NORMALIZED_QTRS,
            next_releases_per_date.index.get_level_values(SID_FIELD_NAME)
        ])

        return next_releases_per_date


class PreviousQuartersEstimatesLoader(QuarterEstimatesLoader):
    def __init__(self,
                 estimates,
                 columns):
        super(PreviousQuartersEstimatesLoader, self).__init__(estimates,
                                                              columns)

    def load_quarters(self, num_quarters, stacked_last_per_qtr):
        # Filter for releases that are on or before each simulation date and
        # determine the previous quarter by picking out the upcoming release
        # for each date in the index.
        stacked_last_per_qtr = stacked_last_per_qtr.sort(EVENT_DATE_FIELD_NAME)
        previous_releases_per_date = stacked_last_per_qtr.loc[
            stacked_last_per_qtr[EVENT_DATE_FIELD_NAME] <=
            stacked_last_per_qtr.index.get_level_values(
                SIMULTATION_DATES
            )].groupby(level=[SIMULTATION_DATES, SID_FIELD_NAME]).nth(-1)
        previous_releases_per_date[
            SHIFTED_NORMALIZED_QTRS
        ] = previous_releases_per_date.index.get_level_values(
            NORMALIZED_QUARTERS
        ) - (num_quarters - 1)
        previous_releases_per_date = previous_releases_per_date.set_index([
            previous_releases_per_date.index.get_level_values(
                SIMULTATION_DATES
            ),
            SHIFTED_NORMALIZED_QTRS,
            previous_releases_per_date.index.get_level_values(SID_FIELD_NAME)
        ])
        return stacked_last_per_qtr.loc[previous_releases_per_date.index]
