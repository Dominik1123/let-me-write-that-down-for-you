import json
import logging
import time

import telepot
from telepot.loop import MessageLoop

from sheets import Sheets
from telegram import Handler


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    with open('config.json') as fh:
        config = json.load(fh)

    bot = telepot.Bot(config['telegram']['token'])
    sheet = Sheets(config['sheets'])
    # # Optionally register callbacks for new accounting periods here:
    # sheet.new_ap_supervisor.register_callback()
    handler = Handler(bot, sheet, config['telegram'])
    loop = MessageLoop(bot, handler.handle)
    loop.run_as_thread()

    while True:
        time.sleep(10)
