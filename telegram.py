from datetime import datetime
import logging
import re
import traceback

import telepot
from urllib3.expcetions import ProtocolError


class Handler:
    replies = {
        'help': {
            'de': 'Hallo {}! Du kannst mir neue Einträge mittels "/new" schicken. Das Format ist wie folgt:\n'
                  '```\n/new <Liste von Namen> <Betrag> <Beschreibung>\n```\n'
                  'Die Namen können mittels Leerzeichen oder "," und "+" separiert sein, der Betrag kann einen '
                  'Dezimalpunkt oder -komma beinhalten. Falls die Beschreibung ein Datum im Format TT.MM.YYYY enthält, '
                  'verwende ich dieses, ansonsten das aktuelle Datum. Falls der Betrag negativ ist, vertausche ich '
                  'Sender und Empfänger.\n'
                  'Mit "/undo" kannst Du den letzten Eintrag wieder löschen und mit "/summary" schicke ich eine '
                  'Zusammenfassung aller Auslagen des aktuellen Zeitraums.',
            'en': 'Hi {}! You can add new records by telling me "/new". The format is as follows:\n'
                  '```\n/new <list-of-names> <amount> <description>\n```\n'
                  'The names can be separated by just whitespace or by either "," or "+". The amount may contain a '
                  'decimal separator in form of a decimal comma or a decimal point. If the description contains a date '
                  'in the format DD.MM.YYYY then it will be used as the record\'s date, otherwise I\'ll use the '
                  'current date. If the amount is negative I\'ll exchange writer and recipient.\n'
                  'With "/undo" you can delete the most recently added record and by telling me "/summary" I\'ll send '
                  'a summary of all expenses of the current period.',
        },
        'unknown_command': {
            'de': 'Das kenne ich nicht',
            'en': 'Unknown command'
        },
        'oops': {
            'de': 'Ups, da ging etwas schief:\n{}',
            'en': 'Oops, something went wrong:\n{}'
        },
        '/new: unknown_format': {
            'de': 'Das habe ich nicht verstanden',
            'en': 'Illegal format'
        },
        '/new: negative_amount_can_refer_to_only_one_debtor': {
            'de': 'Ein negativer Betrag kann nur auf eine einzelne Person bezogen werden',
            'en': 'A negative amount must refer to a single person',
        },
        '/new: success': {
            'de': 'Alles klar, ich habe den folgenden Eintrag angelegt:\n\n{}\n'
                  'Um diesen Eintrag wieder zu löschen, schreibe "/undo".',
            'en': 'All right, I added the following record:\n{}\nTo delete that record just send me "/undo".'
        },
        '/undo: success': {
            'de': 'Alles klar, ich habe den folgenden Eintrag gelöscht:\n\n{}',
            'en': 'All right, I deleted the following record:\n\n{}'
        },
        '/summary: caption': {
            'de': 'Zusammenfassung für {}',
            'en': 'Summary for {}',
        }
    }

    def __init__(self, bot, sheet, config):
        self.bot = bot
        self.chat_id = config['chat_id']
        self.sheet = sheet
        self.replies = {k: v[config['lang']] for k, v in self.replies.items()}
        self.config = config
        self.sheet.new_ap_supervisor.register_callback(self.send_summary)

    def handle(self, msg):
        content_type, chat_type, chat_id = telepot.glance(msg)
        if chat_id == self.chat_id and content_type == 'text':
            cmd = re.match(r'^/([a-z]+)', msg['text'])
            if cmd is not None:
                try:
                    handler = getattr(self, f'_handle_{cmd.group(1)}')
                except AttributeError:
                    logging.info(f'Unknown command: {cmd.group(0)}')
                    self._reply(self.replies['unknown_command'].format(cmd.group(0)))
                else:
                    try:
                        handler(msg)
                    except Exception as err:
                        logging.critical(traceback.format_exc())
                        self._reply(self.replies['oops'].format(str(err)))
        elif chat_id != self.chat_id:
            logging.info(f'Denied message from chat id {chat_id} ({msg})')

    def _handle_new(self, msg):
        from_name = msg['from']['first_name'].lower()
        donor = self.config['aliases'].get(from_name, from_name).capitalize()
        match = re.match(r'^ *((?:[a-z]+ *[,+&]? *)*[a-z]+) *(-?[0-9]+(?:[.,][0-9]+)?) *(.+)?',
                         msg['text'][5:], flags=re.I)
        if match is None:
            self._reply(self.replies['/new: unknown_format'])
        else:
            debtors = [x.capitalize() for x in re.findall(r'[a-z]+', match.group(1), flags=re.I)]
            amount = match.group(2)
            description = match.group(3)
            date = re.findall(r'\d{2}\.\d{2}\.\d{4}', description or '')
            if date:
                description = description.replace(date[0], '').strip()
                date = datetime.strptime(date[0], '%d.%m.%Y')
            else:
                date = datetime.now()
            if amount.startswith('-'):
                if len(debtors) > 1:
                    self._reply(self.replies['/new: negative_amount_can_refer_to_only_one_debtor'])
                    return
                donor, debtors = debtors[0], [donor]
                amount = amount.lstrip('-')
            record = self.sheet.append([date, description, donor, ' + '.join(debtors), amount])
            self._reply(self.replies['/new: success'].format(self._format_record(record)))

    def _handle_undo(self, __):
        try:
            record = self.sheet.undo()
        except IndexError as err:
            self._reply(str(err))
        else:
            self._reply(self.replies['/undo: success'].format(self._format_record(record)))

    def _handle_summary(self, __):
        self.send_summary(self.sheet.summary())

    def _handle_newperiod(self, __):
        self.sheet.new_accounting_period_from_previous_month()
        self.send_summary(self.sheet.summary_previous_month())

    def send_summary(self, summary):
        self.bot.sendDocument(self.chat_id,
                              (self.sheet.current_table_name.lower().replace(' ', '_') + '.html', summary[1]),
                              caption=self.replies['/summary: caption'].format(self.sheet.current_table_name))

    def _handle_help(self, msg):
        self._reply(self.replies['help'].format(msg['from']['first_name']))

    def _handle_ping(self, __):
        self._reply('Hi \N{Waving Hand Sign}')

    def _reply(self, msg_text):
        self.bot.sendMessage(self.chat_id, msg_text, parse_mode='markdown')

    def _send(self, func, *args, **kwargs):
        try:
            func(*args, **kwargs)
        except ProtocolError:
            self.bot = telepot.Bot(self.bot.token)
            self._send(func, *args, **kwargs)

    def _format_record(self, record):
        backticks = '```'
        max_width = max(map(len, self.sheet.columns))
        return (
            f'{backticks}\n'
            + '\n'.join(f'{c.ljust(max_width)} {v}' for c, v in zip(self.sheet.columns, record.data))
            + f'\n{backticks}'
        )
