from functools import partial
from io import BytesIO
import itertools as it
import re
from typing import List, Tuple

from jinja2 import Template
import pandas as pd


class Notes:
    with open('summary.html.j2') as fh:
        template = Template(fh.read())

    def __init__(self, columns):
        self.columns = columns
        self.steps = []

    def add(self, step, caption, index=None, group_by=None):
        step = step.copy()
        if isinstance(step, pd.DataFrame):
            if self.columns[1] in step.columns \
                    and isinstance(step[self.columns[1]].iloc[0], list):
                step[self.columns[1]] = step[self.columns[1]].map(lambda x: ' + '.join(x))
            if self.columns[3] in step.columns:
                step[self.columns[3]] = round_2(step[self.columns[3]])
            if index is not None:
                step = step.set_index(index).sort_index()
            if group_by is not None:
                step = step.sort_values(group_by).groupby(group_by)
        self.steps.append((caption, step))

    def render(self, title):
        html_text = BytesIO()
        html_text.write(
            self.template.render(title=title, steps=[(x[0], self._to_html(x[1])) for x in self.steps]).encode('utf-8'))
        html_text.flush()
        html_text.seek(0)
        return html_text

    @classmethod
    def _to_html(cls, step):
        if isinstance(step, pd.Series):
            name = step.name
            step = step.to_frame()
            step.columns = [name]
        return re.sub('class="dataframe"', 'class="pure-table pure-table-bordered"', step.to_html())


def round_2(obj):
    if isinstance(obj, (pd.DataFrame, pd.core.groupby.DataFrameGroupBy)):
        return obj.applymap(partial(round, ndigits=2))
    elif isinstance(obj, pd.Series):
        return obj.map(partial(round, ndigits=2))
    else:
        raise ValueError(f'Invalid type: {type(obj)}')


def compute_recipients(address: str, groups: pd.DataFrame = None) -> list:
    """Compute the persons for whom a payment was made. Groups will be expanded to their members.

    :param address: str
        Indicates the recipients for this payment, separated by one of ``+&;`` (+ optional whitespace around the
        separator). Persons can be subtracted from the recipients list by using of one ``-\\``.
    :param groups: pd.DataFrame, optional
        Data frame indicating the groups that people may be part of, if any.
    :return list
        List with the names of all persons for whom the payment was made, in alphabetical order.

    >>> import pandas as pd
    >>> g = pd.DataFrame(data=[
    ...     ['x'       , 'x'      ],
    ...     ['x'       , pd.np.nan],
    ...     [pd.np.nan , pd.np.nan],
    ... ], columns=['Ice Cream', 'Pizza'], index=['Alice', 'Bob', 'Charlie'])
    >>> compute_recipients('Alice + Ice Cream + Doris', g)
    ['Alice', 'Bob', 'Doris']
    >>> compute_recipients('Alice & Bob & Charlie', g)
    ['Alice', 'Bob', 'Charlie']
    >>> compute_recipients('Pizza; Ice Cream', g)
    ['Alice', 'Bob']
    >>> compute_recipients('Doris + Ice Cream - Bob', g)
    ['Alice', 'Doris']
    >>> compute_recipients('Ice Cream + Pizza \\ Alice', g)
    ['Bob']
    """

    def _expand_groups(_parts):
        if not _parts:
            return set()
        if groups is None:
            return _parts
        _parts = tuple(_parts)
        relevant_groups = tuple(filter(lambda x: x in groups.columns, _parts))
        relevant_group_members = tuple(it.chain(*map(
            lambda x: list(groups.index[groups[x].notnull()]),
            relevant_groups
        )))
        return (set(_parts) - set(relevant_groups)) | set(relevant_group_members)

    parts = re.split(r'\s*[+&;]\s*', address)
    add = tuple(map(lambda x: re.split(r'\s*[\-\\]\s*', x)[0], parts))
    subtract = tuple(it.chain(*map(lambda x: re.split(r'\s*[\-\\]\s*', x)[1:], parts)))
    return sorted(_expand_groups(add) - _expand_groups(subtract))


def summary(df: pd.DataFrame, title: str = '') -> Tuple[List[pd.DataFrame], BytesIO]:
    """Computes the summary for the given data frame.

    :return tuple
        list of data frame: one data frame for each step; the steps are:
            "outlay, outlay (expanded groups), outlay (single persons), outlay (summed for each pair of persons),
             expenses (per person), balances (per person), clearing",
        BytesIO: containing the HTML version of the summary, UTF-8 encoded
    """
    date, item, creditor, debtor, amount = df.columns
    df = df[[creditor, debtor, item, amount]]
    df = df.applymap(str.strip)
    df[[amount]] = df[[amount]].applymap(float)

    df_groups = pd.DataFrame()

    notes = Notes(df.columns)
    notes.add(df, 'Outlay', index=[creditor, debtor, item])

    df_expanded = df.copy()
    df_expanded[debtor] = df_expanded[debtor].map(partial(compute_recipients, groups=df_groups))
    notes.add(df_expanded, 'Outlay (expanded)', index=[creditor, debtor, item])

    amount_per_person = df_expanded[amount] / df_expanded[debtor].str.len()
    receiving_persons = df_expanded[debtor].apply(pd.Series, 1).stack()
    receiving_persons.index = receiving_persons.index.droplevel(-1)
    receiving_persons.name = debtor
    df_stacked = df_expanded.drop(columns=debtor)
    df_stacked[[amount]] = amount_per_person
    df_stacked = df_stacked.join(receiving_persons)
    df_stacked = df_stacked[[creditor, debtor, item, amount]]
    notes.add(df_stacked, 'Outlay (stacked)', index=[creditor, debtor, item])

    df_summed = df_stacked.drop(columns=item).groupby([creditor, debtor]).sum()
    notes.add(df_summed, 'Outlay (summed)')

    df_expenses = df_summed.unstack(fill_value=0.)
    df_expenses.columns = df_expenses.columns.droplevel(0)
    total_received = df_expenses.sum(axis=0)
    total_received.name = 'Total (received)'
    df_expenses = df_expenses.append(total_received)
    total_paid = df_expenses.sum(axis=1)
    df_expenses['Total (paid)'] = total_paid
    total_paid.drop(index='Total (received)', inplace=True)
    notes.add(round_2(df_expenses), 'Expenses')

    balances = total_paid.subtract(total_received, fill_value=0.)
    balances.name = 'Balance'
    notes.add(round_2(balances), 'Balances')

    transfers = []
    while balances.size > 1:
        creditor = balances.idxmax()
        debtor = balances.idxmin()
        paid = balances[creditor]
        received = abs(balances[debtor])
        transfers.append((debtor, creditor, min(paid, received)))
        if paid >= received:
            balances[creditor] -= received
            balances.drop(index=debtor, inplace=True)
        else:
            balances[debtor] += paid
            balances.drop(index=creditor, inplace=True)

    df_transfers = pd.DataFrame(
        transfers, columns=['Writer', 'Recipient', 'Amount']).set_index(['Writer', 'Recipient']).sort_index()
    notes.add(round_2(df_transfers), 'Clearing')

    return notes.steps, notes.render(title)
