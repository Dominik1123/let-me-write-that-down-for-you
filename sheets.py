from collections import deque, namedtuple
from datetime import datetime, timedelta
import logging
import os.path
import pickle
from threading import Lock, Thread, Timer
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pandas as pd

from stats import summary


class Sheets:
    Record = namedtuple('record', 'table row data')
    carry_over_str = {'de': 'Übertrag {}', 'en': 'carryover {}'}
    messages = {
        'undo: expired': {
            'de': 'Der letzte Eintrag kann nur innerhalb von {undo_timer} Sekunden gelöscht werden.',
            'en': 'The most recent record can only be undone with {undo_timer} seconds.'
        },
        'undo: exceeded': {
            'de': 'Nur der letzte Eintrag kann rückgängig gemacht werden.',
            'en': 'Only the most recent record can be undone.'
        },
        'undo: none': {
            'de': 'Momentan ist kein Eintrag vorhanden, der rückgängig gemacht werden könnte.',
            'en': 'There is no record to be undone at the moment.'
        }
    }
    lock = Lock()

    def __init__(self, config: dict, *, new_ap_callbacks: list = None):
        self.config = config
        self.spreadsheet_id = self.config['spreadsheet_id']
        self.history = deque(maxlen=1)
        self.undo_timer = Timer(0, lambda: None)
        self.carry_over_str = self.carry_over_str[config['lang']]
        self.messages = {reason: lang[config['lang']].format(**config) for reason, lang in self.messages.items()}

        creds = None
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    'credentials.json', config['sheets']['scopes'])
                creds = flow.run_local_server(port=0)
            with open('token.pickle', 'wb') as token:
                pickle.dump(creds, token)

        self.service = build('sheets', 'v4', credentials=creds)
        self.sheet = self.service.spreadsheets()
        self._columns = None

        self.new_ap_supervisor = NewAPSupervisor(self, config, callbacks=new_ap_callbacks)
        self.new_ap_supervisor.start()

    @property
    def columns(self):
        if self._columns is None:
            try:
                self._columns = self.sheet.values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f'{self.current_table_name}!A1:E1'
                ).execute()['values'][0]
            except HttpError as err:
                logging.error('Cannot fetch table for current accounting period: {}'.format(err))
        return self._columns

    @property
    def current_table_name(self):
        return datetime.today().strftime(self.config['table_name_format'])

    def append(self, record: list) -> 'Sheets.Record':
        self.undo_timer.cancel()
        table_name = record[0].strftime(self.config['table_name_format'])
        record[0] = record[0].strftime(self.config['date_format'])
        data = {'range': table_name, 'values': [record]}
        logging.info(f'Submitting update request: {data}')
        result =  self.sheet.values().append(
            spreadsheetId=self.spreadsheet_id, range=data['range'], body=data, valueInputOption='USER_ENTERED',
        ).execute()
        row = result['updates']['updatedRange'].split('!')[1].split(':')[0].lstrip('A')
        item = self.Record(table_name, row, data['values'][0])
        self._history_append(item)
        self.undo_timer = Timer(self.config['undo_timer'], self._history_append, args=(self.messages['undo: expired'],))
        self.undo_timer.start()
        return item

    def undo(self) -> 'Sheets.Record':
        item = self._history_replace(self.messages['undo: exceeded'])
        if item is None or isinstance(item, str):
            raise IndexError(item or self.messages['undo: none'])
        row = item.row
        table_range = f'{item.table}!A{row}:E{row}'
        result = self.sheet.values().get(spreadsheetId=self.spreadsheet_id, range=table_range).execute()
        logging.info(f'Deleting row {row}')
        self.sheet.values().clear(spreadsheetId=self.spreadsheet_id, range=table_range).execute()
        return self.Record(item.table, row, result['values'][0])

    def new_accounting_period(self):
        today = datetime.today()
        tomorrow = today + timedelta(days=1)
        old_table = today.strftime(self.config['table_name_format'])
        new_table = tomorrow.strftime(self.config['table_name_format'])
        date = tomorrow.strftime(self.config['date_format'])
        self._new_accounting_period(old_table, new_table, date)

    def new_accounting_period_from_previous_month(self):
        today = datetime.today()
        previous = today - timedelta(days=today.day)
        date = (previous + timedelta(days=1)).strftime(self.config['date_format'])
        old_table = previous.strftime(self.config['table_name_format'])
        new_table = today.strftime(self.config['table_name_format'])
        self._new_accounting_period(old_table, new_table, date)

    def _new_accounting_period(self, old_table, new_table, date):
        if old_table == new_table:
            raise RuntimeError('Today is not the last day of the accounting period')
        columns = self.sheet.values().get(
            spreadsheetId=self.spreadsheet_id, range=f'{old_table}!A1:E1').execute()['values'][0]
        self.sheet.batchUpdate(spreadsheetId=self.spreadsheet_id,
                               body={'requests': [{'addSheet': {'properties': {'title': new_table}}}]}).execute()
        clearing = self._summary(old_table)[0][-1][1]
        values = [columns] + [
            [date, self.carry_over_str.format(old_table)] + list(i)[::-1] + c.tolist() for i, c in clearing.iterrows()]
        values += [[date] + data for data in self.config['recurring_data']]
        range_ = f'{new_table}!A1:E{len(values)}'
        result = self.sheet.values().update(
            spreadsheetId=self.spreadsheet_id,
            range=range_, body=dict(range=range_, values=values), valueInputOption='USER_ENTERED'
        ).execute()
        logging.info(result)

    def summary(self):
        return self._summary(self.current_table_name)

    def summary_previous_month(self):
        today = datetime.today()
        previous = today - timedelta(days=today.day)
        return self._summary(previous.strftime(self.config['table_name_format']))

    def _summary(self, table_name):
        values = self.sheet.values().get(
            spreadsheetId=self.spreadsheet_id, range=table_name).execute()['values']
        df = pd.DataFrame(data=values[1:], columns=values[0], index=range(len(values)-1), dtype=str)
        return summary(df, table_name)

    def _history_append(self, item):
        with self.lock:
            self.history.append(item)

    def _history_pop(self):
        with self.lock:
            try:
                return self.history.pop()
            except IndexError:
                return None

    def _history_replace(self, item):
        with self.lock:
            try:
                old = self.history.pop()
            except IndexError:
                old = None
            self.history.append(item)
            return old


class NewAPSupervisor(Thread):
    """Supervisor for creating new accounting periods."""
    lock = Lock()

    def __init__(self, sheet: Sheets, config: dict, *, callbacks: list = None):
        super().__init__()
        self.sheet = sheet
        self.config = config
        self.summary_time = [int(x) for x in config['create_summary_at'].split(':')]
        self.callbacks = callbacks or []
        self.ap_done = deque(maxlen=1)

    def run(self) -> None:
        while True:
            time.sleep(10)
            today_str = self.sheet.current_table_name
            tomorrow_str = (datetime.today() + timedelta(days=1)).strftime(self.config['table_name_format'])
            if tomorrow_str != today_str and today_str not in self.ap_done:
                now = datetime.now()
                if now.hour == self.summary_time[0] and now.minute >= self.summary_time[1]:
                    logging.info(f'Create new accounting period: {tomorrow_str}')
                    self.ap_done.append(today_str)
                    self.sheet.new_accounting_period()
                    result = self.sheet.summary()
                    with self.lock:
                        for callback in self.callbacks:
                            callback(result)

    def register_callback(self, callback):
        """Register a new callback. The argument to the callback is the return value of `stats.summary`."""
        with self.lock:
            self.callbacks.append(callback)
