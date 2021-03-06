from plugins.base import PluginProcessBase, Process
from plugins.decorators import make_plugin
from plugins.processes_host import find_plugin, active_plugins
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, BaseFilter
from telegram import error as terr, PhotoSize
from specialized.telegram_support.auth import AuthStatus, AuthAttemptResult
import logging
from specialized.telegram_support.auth_io import save_chat_auth_storage, load_chat_auth_storage
from specialized.telegram_support.handlers import make_handler as _make_handler, HandlerBase
from misc.settings import SETTINGS
from misc.logging import ensure_logging_setup, camel_to_snake
from Pyro4 import expose as pyro_expose, oneway as pyro_oneway
from time import sleep
from specialized.support.txtutils import user_desc
import os
from specialized.plugin_status_led import Status


TELEGRAM_ROOT_PLUGIN_NAME = 'TelegramRoot'
ensure_logging_setup()
_log = logging.getLogger(camel_to_snake(TELEGRAM_ROOT_PLUGIN_NAME))
_TELEGRAM_RETRY_CAP_SECONDS = 10


def extract_largest_photo_size(photo_sizes):
    return max(photo_sizes, key=lambda photo_size: photo_size.width * photo_size.height)


class AuthStatusFilter(BaseFilter):
    def __init__(self, chat_auth_storage, status):
        self.chat_auth_storage = chat_auth_storage
        self.requested_status = status

    def filter(self, message):
        return self.chat_auth_storage.has_auth_status(message.chat_id, self.requested_status)


def _normalize_filters(some_telegram_plugin, filters, auth_status=None):
    if auth_status is not None:
        if filters is None:
            return some_telegram_plugin.root_telegram_plugin.auth_filters[auth_status]
        else:
            return filters & some_telegram_plugin.root_telegram_plugin.auth_filters[auth_status]
    return filters


def handle_command(command, pass_args=False, filters=None, auth_status=AuthStatus.AUTHORIZED):
    def _make_command_handler(some_telegram_plugin_, callback_, command_, pass_args_, filters_, auth_status_):
        # Enforce the handlers to use the RootTelegramPlugin, so that we get nicer error handling
        def _drop_bot_in_call(_, update, *args, **kwargs):
            return callback_(update, *args, **kwargs)
        return CommandHandler(command_, _drop_bot_in_call,
                              filters=_normalize_filters(some_telegram_plugin_, filters_, auth_status=auth_status_),
                              pass_args=pass_args_)

    return _make_handler(_make_command_handler, command, pass_args, filters, auth_status)


def handle_message(filters=None, auth_status=AuthStatus.AUTHORIZED):
    def _make_message_handler(some_telegram_plugin_, callback_, filters_, auth_status_):
        # Enforce the handlers to use the RootTelegramPlugin, so that we get nicer error handling
        def _drop_bot_in_call(_, update, *args, **kwargs):
            return callback_(update, *args, **kwargs)
        return MessageHandler(_normalize_filters(some_telegram_plugin_, filters_, auth_status=auth_status_),
                              _drop_bot_in_call)

    return _make_handler(_make_message_handler, filters, auth_status)


class TelegramProcessBase(PluginProcessBase, HandlerBase):
    @classmethod
    def process(cls):  # pragma: no cover
        return Process.TELEGRAM

    @property
    def root_telegram_plugin(self):
        return find_plugin(TELEGRAM_ROOT_PLUGIN_NAME).telegram


@make_plugin(TELEGRAM_ROOT_PLUGIN_NAME, Process.TELEGRAM)
class TelegramRootPlugin(TelegramProcessBase):
    def _save_chat_auth_storage(self):
        _log.info('Saving auth storage to %s', os.path.realpath(SETTINGS.telegram.get('auth_file', cast_to_type=str)))
        save_chat_auth_storage(SETTINGS.telegram.get('auth_file', cast_to_type=str), self._auth_storage, log=_log)

    def _setup_handlers(self):
        def _collect_handlers():
            for plugin in active_plugins():
                if plugin.telegram is None or not isinstance(plugin.telegram, TelegramProcessBase):
                    continue
                yield from plugin.telegram.handlers
        if len(self._updater.dispatcher.handlers) > 0:
            return None
        cnt = 0
        for handler in _collect_handlers():
            self._updater.dispatcher.add_handler(handler)
            cnt += 1
        return cnt

    @staticmethod
    def _broadcast_media(method, chat_ids, media_obj, *args, **kwargs):
        retval = []
        file_id = None
        for chat_id in chat_ids:
            _log.info('Sending media %s to %d.', str(media_obj), chat_id)
            if file_id is None:
                _log.info('Beginning upload of media %s...', str(media_obj))
                msg = method(chat_id, media_obj, *args, **kwargs)
                if msg:
                    attachment = msg.effective_attachment
                    if isinstance(attachment, list) and len(attachment) > 0 and isinstance(attachment[0], PhotoSize):
                        file_id = extract_largest_photo_size(attachment).file_id
                    else:
                        file_id = attachment.file_id
                    _log.info('Media %s uploaded as file id %s...', str(media_obj), str(file_id))
                else:
                    _log.error('Unable to send media %s.', str(media_obj))
                    return
                retval.append(msg)
            else:
                retval.append(method(chat_id, media_obj, *args, **kwargs))
        return retval

    def _send(self, method, chat_id, *args, retries=3, **kwargs):
        for i in range(retries):
            # noinspection PyBroadException
            try:
                if i > 0:
                    _log.info('Retrying %d/%d...', i + 1, retries)
                with Status.pulse((0, 0, 1)):
                    return method(chat_id, *args, **kwargs)
            except terr.TimedOut as e:
                _log.error('Telegram timed out when executing %s: %s.', str(method), e.message)
            except terr.RetryAfter as e:
                _log.error('Telegram requested to retry %s in %d seconds.', str(method), e.retry_after)
                if e.retry_after > _TELEGRAM_RETRY_CAP_SECONDS:
                    _log.info('Will sleep for %d seconds only.', _TELEGRAM_RETRY_CAP_SECONDS)
                sleep(min(_TELEGRAM_RETRY_CAP_SECONDS, e.retry_after))
            except terr.InvalidToken:
                _log.error('Invalid Telegram token. Will not retry %s.', str(method))
                break
            except terr.BadRequest as e:
                _log.error('Bad request when performing %s: %s. Will not retry.', str(method), e.message)
                break
            except terr.NetworkError as e:
                _log.error('Network error when running %s: %s.', str(method), e.message)
                sleep(1)
            except terr.Unauthorized as e:
                _log.error('Not authorized to perform %s: %s. Will not retry.', str(method), e.message)
                break
            except terr.ChatMigrated as e:
                _log.warning('Chat %d moved to new chat id %d. Will update and retry.', chat_id, e.new_chat_id)
                self._auth_storage.replace_chat_id(chat_id, e.new_chat_id)
                return self._send(method, e.new_chat_id, *args, retries=retries, **kwargs)
            except terr.TelegramError as e:
                _log.error('Generic Telegram error when performing %s: %s.', str(method), e.message)
                sleep(1)
            except:
                _log.exception('Error when performing %s.', str(method))
        Status.blink((1, 0.8, 0), n=1)
        return None

    @property
    def auth_filters(self):
        return self._auth_filters

    @pyro_expose
    @property
    def authorized_chat_ids(self):
        return list(map(lambda chat: chat.chat_id, self._auth_storage.authorized_chats))

    @pyro_expose
    @pyro_oneway
    def send_photo(self, chat_id, photo, *args, retries=3, **kwargs):
        return self._send(self._updater.bot.send_photo, chat_id, photo, *args, retries=retries, **kwargs)

    @pyro_expose
    @pyro_oneway
    def send_video(self, chat_id, video, *args, retries=3, **kwargs):
        return self._send(self._updater.bot.send_video, chat_id, video, *args, retries=retries, **kwargs)

    @pyro_expose
    @pyro_oneway
    def send_message(self, chat_id, message, *args, retries=3, **kwargs):
        return self._send(self._updater.bot.send_message, chat_id, message, *args, retries=retries, **kwargs)

    @pyro_expose
    @pyro_oneway
    def broadcast_photo(self, chat_ids, photo, *args, retries=3, **kwargs):
        return TelegramRootPlugin._broadcast_media(self.send_photo, chat_ids, photo, *args, retries=retries, **kwargs)

    @pyro_expose
    @pyro_oneway
    def broadcast_video(self, chat_ids, video, *args, retries=3, **kwargs):
        return TelegramRootPlugin._broadcast_media(self.send_video, chat_ids, video, *args, retries=retries, **kwargs)

    @pyro_expose
    @pyro_oneway
    def broadcast_message(self, chat_ids, message, *args, retries=3, **kwargs):
        return list([self.send_message(chat_id, message, *args, retries=retries, **kwargs) for chat_id in chat_ids])

    @pyro_expose
    @pyro_oneway
    def reply_message(self, update, message, *args, retries=3, **kwargs):
        return self.send_message(update.effective_chat.id, message, *args,
                                 retries=retries, reply_to_message_id=update.message.message_id, **kwargs)

    @pyro_expose
    @pyro_oneway
    def reply_photo(self, update, photo, *args, retries=3, **kwargs):
        return self.send_photo(update.effective_chat.id, photo, *args,
                               retries=retries, reply_to_message_id=update.message.message_id, **kwargs)

    @pyro_expose
    @pyro_oneway
    def reply_video(self, update, video, *args, retries=3, **kwargs):
        return self.send_video(update.effective_chat.id, video, *args,
                               retries=retries, reply_to_message_id=update.message.message_id, **kwargs)

    def __init__(self):
        super(TelegramRootPlugin, self).__init__()
        self._updater = Updater(token=SETTINGS.telegram.get('token', cast_to_type=str))
        self._auth_storage = load_chat_auth_storage(SETTINGS.telegram.get('auth_file', cast_to_type=str), log=_log)
        self._auth_filters = dict({
            status: AuthStatusFilter(self._auth_storage, status) for status in AuthStatus
        })

    def __enter__(self):
        super(TelegramRootPlugin, self).__enter__()
        _log.info('Setting up Telegram handlers...')
        cnt_handlers = self._setup_handlers()
        if cnt_handlers is None:
            _log.info('Handlers already registered. Beginning serving...')
        else:
            _log.info('Registered %d handler(s). Beginning serving...', cnt_handlers)
        self._updater.start_polling(poll_interval=1, timeout=20, clean=True)
        _log.info('Telegram bot is being served.')
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        super(TelegramRootPlugin, self).__exit__(exc_type, exc_val, exc_tb)
        _log.info('Stopping serving Telegram bot...')
        self._updater.stop()
        _log.info('Telegram bot was stopped.')

    @handle_command('start', auth_status=AuthStatus.UNKNOWN)
    def _bot_start_new_chat(self, upd):
        _log.info('Access requested by %s', user_desc(upd))
        password = self._auth_storage[upd.effective_chat.id].start_auth(user_desc(upd))
        self._save_chat_auth_storage()
        print('\n\nChat ID: %d, User: %s, Password: %s\n\n' % (upd.effective_chat.id, user_desc(upd), password))
        self.send_message(upd.effective_chat.id, 'Reply with the pass that you can read on the console.')

    @handle_command('start', auth_status=AuthStatus.ONGOING)
    def _bot_start_resume_auth(self, upd):
        _log.info('Authentication resumed for chat %d, user %s.', upd.effective_chat.id, user_desc(upd))
        self.send_message(upd.effective_chat.id, 'Reply with the pass that you can read on the console.')

    @handle_command('start', auth_status=AuthStatus.AUTHORIZED)
    def _bot_start(self, upd):
        _log.info('Started on chat %d', upd.effective_chat.id)
        self.send_message(upd.effective_chat.id, 'Ratcam is active.')

    @handle_command('logout', auth_status=AuthStatus.AUTHORIZED)
    def _bot_logout(self, upd):
        _log.info('Exiting chat %d (%s).', upd.effective_chat.id, str(upd.effective_chat.title))
        self._auth_storage[upd.effective_chat.id].revoke_auth()
        self._save_chat_auth_storage()

    @handle_message(Filters.text, auth_status=AuthStatus.ONGOING)
    def _bot_try_auth(self, upd):
        password = upd.message.text
        result = self._auth_storage[upd.effective_chat.id].try_auth(password)
        self._save_chat_auth_storage()
        if result == AuthAttemptResult.AUTHENTICATED:
            self.reply_message(upd, 'Authenticated.')
        elif result == AuthAttemptResult.WRONG_TOKEN:
            self.reply_message(upd, 'Incorrect password.')
        elif result == AuthAttemptResult.EXPIRED:
            self.reply_message(upd, 'Your password expired.')
        elif result == AuthAttemptResult.TOO_MANY_RETRIES:
            self.reply_message(upd, 'Number of attempts exceeded.')
        _log.info('Authentication attempt for chat %d, user %s, outcome: %s', upd.effective_chat.id, user_desc(upd),
                  result)

    @handle_message(Filters.status_update.left_chat_member, auth_status=None)
    def _bot_user_left(self, upd):
        if upd.effective_chat.get_members_count() <= 1:
            _log.info('Exiting chat %d (%s).', upd.effective_chat.id, str(upd.effective_chat.title))
            self._auth_storage[upd.effective_chat.id].revoke_auth()
            self._save_chat_auth_storage()
