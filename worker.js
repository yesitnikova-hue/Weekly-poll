const API = (env, method, body) => fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
  method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)
}).then(async r => { const data = await r.json(); if (!data.ok) throw new Error('Telegram rejected request'); return data.result; });

const owner = env => String(env.OWNER_TELEGRAM_ID);
const key = env => `owner:${owner(env)}`;
const read = async env => (await env.POLL_STATE.get(key(env), 'json')) || {};
const write = (env, value) => env.POLL_STATE.put(key(env), JSON.stringify(value));
const say = (env, text, reply_markup) => API(env, 'sendMessage', {chat_id: Number(owner(env)), text, ...(reply_markup && {reply_markup})});
const buttons = rows => ({inline_keyboard: rows});
const nonce = () => crypto.randomUUID().replaceAll('-', '');

async function preview(env, draft) {
  const options = draft.options.map((value, index) => `${index + 1}. ${value}`).join('\n');
  await say(env, `Предпросмотр\nКуда: ${draft.target}\n\n${draft.question}\n\nУсловия: ${draft.conditions || 'нет'}\n\n${options}\n\n${draft.anonymous ? 'Анонимно' : 'Имена видны'}; ${draft.multiple ? 'несколько ответов' : 'один ответ'}.\nОпрос будет открыт до ручного закрытия в Telegram.`, buttons([
    [{text: '✅ Опубликовать', callback_data: `publish:${draft.nonce}`}],
    [{text: 'Анонимность ↔', callback_data: `anonymous:${draft.nonce}`}, {text: 'Несколько ответов ↔', callback_data: `multiple:${draft.nonce}`}],
    [{text: '✏️ Начать заново', callback_data: `new:${draft.nonce}`}, {text: 'Отмена', callback_data: `cancel:${draft.nonce}`}]
  ]));
}

async function start(env, state) {
  if (state.target == null) return say(env, 'Сначала задайте место публикации: /target @имя_канала. Бот должен быть администратором.');
  state.draft = {step: 'question', target: state.target, anonymous: true, multiple: false};
  await write(env, state); await say(env, 'Пришлите вопрос (до 300 символов). /cancel — отмена.');
}

async function handleMessage(env, message) {
  if (String(message.from?.id) !== owner(env) || String(message.chat?.id) !== owner(env)) return;
  const text = message.text?.trim(); if (!text) return say(env, 'Пришлите текстом. Голосовые сообщения пока не поддерживаются.');
  const state = await read(env); const command = text.split(/\s+/)[0].split('@')[0];
  if (command === '/start' || command === '/help') return say(env, 'Создайте голосование: /new\nКанал или группа: /target @имя_канала\nПредпросмотр: /preview\nОтмена: /cancel\nНапоминания: /remind_on и /remind_off\nВопрос, условия и варианты вводятся по шагам. Публикация — только кнопкой подтверждения.');
  if (command === '/new') return start(env, state);
  if (command === '/cancel') { delete state.draft; await write(env, state); return say(env, 'Черновик отменён.'); }
  if (command === '/remind_on' || command === '/remind_off') { state.reminders = command === '/remind_on'; await write(env, state); return say(env, `Еженедельные напоминания ${state.reminders ? 'включены.' : 'выключены.'}`); }
  if (command === '/preview') return state.draft?.step === 'preview' ? preview(env, state.draft) : say(env, 'Готового черновика нет. /new — создать новый.');
  if (command === '/target') {
    const value = text.split(/\s+/)[1]; if (!value || !(value.startsWith('@') || /^-?\d+$/.test(value))) return say(env, 'Формат: /target @имя_канала или /target -1001234567890');
    try { const chat = await API(env, 'getChat', {chat_id: /^-?\d+$/.test(value) ? Number(value) : value}); const me = await API(env, 'getMe', {}); const member = await API(env, 'getChatMember', {chat_id: chat.id, user_id: me.id});
      if (!['group', 'supergroup', 'channel'].includes(chat.type) || member.status !== 'administrator' || (chat.type === 'channel' && !member.can_post_messages)) throw new Error();
      state.target = chat.id; await write(env, state); return say(env, 'Место публикации сохранено. Новые черновики используют его.');
    } catch { return say(env, 'Не удалось проверить доступ. Добавьте бота администратором с правом публикации.'); }
  }
  if (text.startsWith('/')) return say(env, 'Неизвестная команда. /help');
  const draft = state.draft; if (!draft) return say(env, 'Начните с /new.');
  if (draft.step === 'question') { if (text.length > 300) return say(env, 'Вопрос: максимум 300 символов. Повторите.'); draft.question = text; draft.step = 'conditions'; await write(env, state); return say(env, 'Пришлите условия (до 1500 символов) или «-», если условий нет.'); }
  if (draft.step === 'conditions') { if (text.length > 1500) return say(env, 'Условия: максимум 1500 символов. Повторите.'); draft.conditions = text === '-' ? '' : text; draft.step = 'options'; await write(env, state); return say(env, 'Пришлите от 2 до 10 вариантов одним сообщением, каждый с новой строки (до 100 символов).'); }
  if (draft.step === 'options') { const options = text.split('\n').map(x => x.trim()).filter(Boolean); if (options.length < 2 || options.length > 10 || options.some(x => x.length > 100) || new Set(options).size !== options.length) return say(env, 'Нужно 2–10 разных вариантов, каждый с новой строки и не длиннее 100 символов.'); draft.options = options; draft.step = 'preview'; draft.nonce = nonce(); await write(env, state); return preview(env, draft); }
  return say(env, 'Используйте кнопки предпросмотра или /new.');
}

async function handleCallback(env, callback) {
  if (String(callback.from?.id) !== owner(env) || String(callback.message?.chat?.id) !== owner(env)) return;
  await API(env, 'answerCallbackQuery', {callback_query_id: callback.id}); const [action, token] = callback.data?.split(':') || []; const state = await read(env); const draft = state.draft;
  if (!draft || token !== draft.nonce || draft.step !== 'preview') return say(env, 'Эта кнопка устарела. Используйте /preview или /new.');
  if (action === 'cancel') { delete state.draft; await write(env, state); return say(env, 'Черновик отменён.'); }
  if (action === 'new') return start(env, state);
  if (action === 'anonymous' || action === 'multiple') { draft[action] = !draft[action]; draft.nonce = nonce(); await write(env, state); return preview(env, draft); }
  if (action === 'publish') { draft.step = 'publishing'; await write(env, state); try { if (draft.conditions) await API(env, 'sendMessage', {chat_id: draft.target, text: `Условия голосования:\n${draft.conditions}`}); const result = await API(env, 'sendPoll', {chat_id: draft.target, question: draft.question, options: draft.options, is_anonymous: draft.anonymous, allows_multiple_answers: draft.multiple}); draft.step = 'published'; draft.message_id = result.message_id; await write(env, state); return say(env, '✅ Опрос опубликован. Следующий: /new'); } catch { draft.step = 'uncertain'; await write(env, state); return say(env, 'Публикация не подтверждена. Проверьте канал: условия могли отправиться отдельно. Автоповтора нет, чтобы избежать дублей.'); } }
}

async function ensureWebhook(env) { if (!env.WEBHOOK_URL) return; const info = await API(env, 'getWebhookInfo', {}); if (info.url !== env.WEBHOOK_URL) await API(env, 'setWebhook', {url: env.WEBHOOK_URL, secret_token: env.TELEGRAM_WEBHOOK_SECRET, allowed_updates: ['message', 'callback_query'], drop_pending_updates: false}); }
async function reminder(env) { const state = await read(env); if (!state.reminders) return; const local = new Intl.DateTimeFormat('en-CA', {timeZone: env.TZ || 'Asia/Yekaterinburg', weekday: 'short', hour: '2-digit', hour12: false, year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(new Date()).reduce((a, x) => ({...a, [x.type]: x.value}), {}); const stamp = `${local.year}-${local.month}-${local.day}`; if (local.weekday === 'Mon' && Number(local.hour) >= 10 && state.reminded !== stamp) { state.reminded = stamp; await write(env, state); await say(env, 'Пора подготовить еженедельное голосование. /new\nБез вашего подтверждения ничего не публикуется.'); } }

export default { async fetch(request, env) { const url = new URL(request.url); if (url.pathname === '/health') return new Response('ok'); if (url.pathname !== '/webhook' || request.method !== 'POST' || request.headers.get('X-Telegram-Bot-Api-Secret-Token') !== env.TELEGRAM_WEBHOOK_SECRET) return new Response('Not found', {status: 404}); try { const update = await request.json(); if (update.message) await handleMessage(env, update.message); if (update.callback_query) await handleCallback(env, update.callback_query); return new Response('ok'); } catch { return new Response('error', {status: 500}); } }, async scheduled(_, env) { try { await ensureWebhook(env); await reminder(env); } catch {} } };
