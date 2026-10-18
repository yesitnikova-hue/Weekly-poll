"""Weekly polls: standard library only; never logs Telegram URLs or credentials."""
import datetime as dt
import json
import logging
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


class TelegramError(Exception):
    pass


class API:
    def __init__(self, token):
        self.url = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, **data):
        request = urllib.request.Request(self.url + method, json.dumps(data).encode(),
                                         {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise TelegramError('Telegram HTTP ' + str(exc.code)) from None
        except (OSError, ValueError):
            raise TelegramError('Нет подтверждения ответа Telegram') from None
        if not result.get('ok'):
            raise TelegramError('Telegram отклонил запрос')
        return result['result']


class Bot:
    def __init__(self, api, owner, db_path, timezone='Europe/Moscow', weekday=0, hour=10):
        self.api, self.owner = api, owner
        self.zone, self.weekday, self.hour = ZoneInfo(timezone), weekday, hour
        self.db = sqlite3.connect(db_path)
        self.db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))

    def say(self, text, buttons=None):
        data = dict(chat_id=self.owner, text=text)
        if buttons:
            data['reply_markup'] = {'inline_keyboard': buttons}
        return self.api.call('sendMessage', **data)

    def preview(self, draft):
        options = '\n'.join(f'{i}. {v}' for i, v in enumerate(draft['options'], 1))
        text = (f"Предпросмотр\nКуда: {draft['target']}\n\n{draft['question']}\n\n"
                f"Условия: {draft['conditions'] or 'нет'}\n\n{options}\n\n"
                f"{'Анонимно' if draft['anonymous'] else 'Имена видны'}; "
                f"{'несколько ответов' if draft['multiple'] else 'один ответ'}.\n"
                'Опрос будет открыт до ручного закрытия в Telegram.')
        nonce = draft['nonce']
        self.say(text, [[{'text': '✅ Опубликовать', 'callback_data': 'publish:' + nonce}],
                        [{'text': 'Анонимность ↔', 'callback_data': 'anonymous:' + nonce},
                         {'text': 'Несколько ответов ↔', 'callback_data': 'multiple:' + nonce}],
                        [{'text': '✏️ Начать заново', 'callback_data': 'new:' + nonce},
                         {'text': 'Отмена', 'callback_data': 'cancel:' + nonce}]])

    def handle(self, update):
        callback = update.get('callback_query')
        msg = update.get('message', {})
        if callback:
            if callback['from']['id'] != self.owner or callback.get('message', {}).get('chat', {}).get('id') != self.owner:
                return
            self.api.call('answerCallbackQuery', callback_query_id=callback['id'])
            action, _, nonce = callback.get('data', '').partition(':')
            draft = self.get('draft')
            if not draft or nonce != draft.get('nonce') or draft.get('step') != 'preview':
                self.say('Эта кнопка устарела. Используйте /preview или /new.')
                return
            if action == 'cancel':
                self.put('draft', None)
                self.say('Черновик отменён.')
            elif action == 'new':
                self.start()
            elif action in ('anonymous', 'multiple'):
                draft[action] = not draft[action]
                draft['nonce'] = secrets.token_hex(8)
                self.put('draft', draft)
                self.preview(draft)
            elif action == 'publish':
                # Save before any network call. Never automatically resend an uncertain publication.
                draft['step'] = 'publishing'
                self.put('draft', draft)
                try:
                    if draft['conditions']:
                        self.api.call('sendMessage', chat_id=draft['target'], text='Условия голосования:\n' + draft['conditions'])
                    result = self.api.call('sendPoll', chat_id=draft['target'], question=draft['question'],
                                           options=[{'text': v} for v in draft['options']],
                                           is_anonymous=draft['anonymous'], allows_multiple_answers=draft['multiple'])
                except TelegramError:
                    draft['step'] = 'uncertain'
                    self.put('draft', draft)
                    self.say('Публикация не подтверждена. Проверьте канал: условия могли отправиться отдельно. '
                             'Автоповтора нет, чтобы избежать дублей. После проверки создайте /new.')
                    return
                draft.update(step='published', message_id=result['message_id'])
                self.put('draft', draft)
                self.say('✅ Опрос опубликован. Следующий: /new')
            return
        if msg.get('from', {}).get('id') != self.owner or msg.get('chat', {}).get('id') != self.owner:
            return
        text = msg.get('text', '').strip()
        if not text:
            self.say('Пришлите текстом. Голосовые сообщения пока не поддерживаются.')
            return
        command = text.split()[0].split('@')[0]
        if command in ('/start', '/help'):
            self.say('Создайте голосование: /new\nКанал или группа: /target @имя_канала '
                     '(или числовой ID)\nПредпросмотр: /preview\nОтмена: /cancel\n'
                     'Напоминания: /remind_on и /remind_off\n'
                     'Вопрос, условия и варианты вводятся по шагам. Публикация — только кнопкой подтверждения.')
        elif command == '/target':
            parts = text.split()
            if len(parts) != 2 or not (parts[1].startswith('@') or parts[1].lstrip('-').isdigit()):
                self.say('Формат: /target @имя_канала или /target -1001234567890')
                return
            target = int(parts[1]) if parts[1].lstrip('-').isdigit() else parts[1]
            try:
                chat = self.api.call('getChat', chat_id=target)
                me = self.api.call('getMe')
                member = self.api.call('getChatMember', chat_id=chat['id'], user_id=me['id'])
                if chat['type'] not in ('group', 'supergroup', 'channel') or member['status'] != 'administrator':
                    raise TelegramError()
                if chat['type'] == 'channel' and not member.get('can_post_messages'):
                    raise TelegramError()
            except TelegramError:
                self.say('Не удалось проверить доступ. Добавьте бота администратором с правом публикации.')
                return
            self.put('target', chat['id'])
            self.say('Место публикации сохранено. Новые черновики используют его; текущий сохраняет прежнее место.')
        elif command == '/new':
            self.start()
        elif command == '/cancel':
            self.put('draft', None)
            self.say('Черновик отменён.')
        elif command in ('/remind_on', '/remind_off'):
            self.put('reminders', command == '/remind_on')
            self.say('Еженедельные напоминания ' + ('включены.' if command == '/remind_on' else 'выключены.'))
        elif command == '/preview':
            draft = self.get('draft')
            if draft and draft['step'] == 'preview':
                self.preview(draft)
            else:
                self.say('Готового черновика нет. /new — создать новый.')
        elif text.startswith('/'):
            self.say('Неизвестная команда. /help')
        else:
            self.advance(text)

    def start(self):
        target = self.get('target')
        if target is None:
            self.say('Сначала задайте место публикации: /target @имя_канала. Бот должен быть администратором.')
            return
        self.put('draft', dict(step='question', target=target, anonymous=True, multiple=False))
        self.say('Пришлите вопрос (до 300 символов). /cancel — отмена.')

    def advance(self, text):
        draft = self.get('draft')
        if not draft:
            self.say('Начните с /new.')
            return
        if draft['step'] == 'question':
            if len(text) > 300:
                self.say('Вопрос: максимум 300 символов. Повторите.')
                return
            draft.update(question=text, step='conditions')
            self.say('Пришлите условия (до 1500 символов) или «-», если условий нет.')
        elif draft['step'] == 'conditions':
            if len(text) > 1500:
                self.say('Условия: максимум 1500 символов. Повторите.')
                return
            draft.update(conditions='' if text == '-' else text, step='options')
            self.say('Пришлите от 2 до 10 вариантов одним сообщением, каждый с новой строки (до 100 символов).')
        elif draft['step'] == 'options':
            options = [v.strip() for v in text.splitlines() if v.strip()]
            if not 2 <= len(options) <= 10 or any(len(v) > 100 for v in options) or len(set(options)) != len(options):
                self.say('Нужно 2–10 разных вариантов, каждый с новой строки и не длиннее 100 символов.')
                return
            draft.update(options=options, step='preview', nonce=secrets.token_hex(8))
            self.put('draft', draft)
            self.preview(draft)
            return
        else:
            self.say('Используйте кнопки предпросмотра или /new.')
            return
        self.put('draft', draft)

    def remind(self, now=None):
        now = now or dt.datetime.now(self.zone)
        week = now.strftime('%G-%V')
        if (self.get('reminders', False) and now.weekday() == self.weekday and now.hour >= self.hour
                and self.get('reminded_week') != week):
            self.put('reminded_week', week)
            self.say('Пора подготовить еженедельное голосование. /new\nБез вашего подтверждения ничего не публикуется.')


def main():
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    owner = os.environ.get('OWNER_TELEGRAM_ID', '')
    if not token or not owner.isdigit() or int(owner) <= 0:
        raise SystemExit('Задайте TELEGRAM_BOT_TOKEN и положительный OWNER_TELEGRAM_ID в защищённых переменных хостинга.')
    path = os.environ.get('DATABASE_PATH', '/data/polls.sqlite3')
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    weekday, hour = int(os.environ.get('REMINDER_WEEKDAY', '0')), int(os.environ.get('REMINDER_HOUR', '10'))
    if not 0 <= weekday <= 6 or not 0 <= hour <= 23:
        raise SystemExit('REMINDER_WEEKDAY: 0–6; REMINDER_HOUR: 0–23')
    api = API(token)
    bot = Bot(api, int(owner), path, os.environ.get('TZ', 'Europe/Moscow'), weekday, hour)
    # Do not silently delete an existing webhook or discard pending updates.
    if api.call('getWebhookInfo').get('url'):
        raise SystemExit('У бота уже настроен webhook. Отключите его перед запуском polling.')
    api.call('setMyCommands', commands=[{'command': c, 'description': d} for c, d in
             [('new', 'Создать голосование'), ('preview', 'Предпросмотр'), ('target', 'Место публикации'),
              ('cancel', 'Отмена'), ('remind_on', 'Включить напоминания'), ('remind_off', 'Выключить напоминания'), ('help', 'Помощь')]])
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    while True:
        try:
            updates = api.call('getUpdates', offset=bot.get('offset', 0), timeout=25,
                               allowed_updates=['message', 'callback_query'])
            for update in updates:
                # Consume first: failures must never replay a publication callback.
                bot.put('offset', update['update_id'] + 1)
                bot.handle(update)
            bot.remind()
        except TelegramError:
            logging.warning('Telegram недоступен или отклонил запрос; повтор через 5 секунд')
            time.sleep(5)


if __name__ == '__main__':
    main()
