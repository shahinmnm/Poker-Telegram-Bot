#!/usr/bin/env python3
import logging
import threading
import time
import traceback
import re
import redis
import inspect

from typing import Callable, Optional
from telegram import Bot
from telegram.utils.request import Request
from telegram.ext import Updater
from telegram.error import (
    TimedOut,
    NetworkError,
    RetryAfter,
    BadRequest,
    Conflict,
    Unauthorized,
)

from pokerapp.config import Config
from pokerapp.pokerbotcontrol import PokerBotCotroller
from pokerapp.pokerbotmodel import PokerBotModel
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.entities import ChatId
from pokerapp.message_delete_manager import MessageDeleteManager


class PokerBot:
    def __init__(self, token: str, cfg: Config, mdm: Optional[MessageDeleteManager] = None, **kw):
        req = Request(con_pool_size=8)
        bot = MessageDelayBot(token=token, request=req)

        self._updater = Updater(bot=bot, use_context=True)

        kv = redis.Redis(
            host=cfg.REDIS_HOST,
            port=cfg.REDIS_PORT,
            db=cfg.REDIS_DB,
            password=cfg.REDIS_PASS if cfg.REDIS_PASS != "" else None,
        )

        self._mdm = mdm or self._build_mdm(cfg)
        bot._mdm = self._mdm

        self._view = PokerBotViewer(bot=bot, mdm=self._mdm, cfg=cfg)
        self._model = PokerBotModel(view=self._view, bot=bot, kv=kv, cfg=cfg, mdm=self._mdm)
        self._controller = PokerBotCotroller(self._model, self._updater, mdm=self._mdm)

        bot.run_tasks_manager()

    def _build_mdm(self, cfg: Config) -> Optional[MessageDeleteManager]:
        try:
            sig = inspect.signature(MessageDeleteManager.__init__)
            kwargs = {}
            if "default_ttl" in sig.parameters:
                kwargs["default_ttl"] = getattr(cfg, "DEFAULT_DELETE_TTL_SECONDS", None)
            if "job_queue" in sig.parameters:
                kwargs["job_queue"] = self._updater.job_queue
            if "bot" in sig.parameters:
                kwargs["bot"] = self._updater.bot
            try:
                return MessageDeleteManager(**kwargs)
            except TypeError:
                return MessageDeleteManager()
        except Exception:
            logging.info("MDM unavailable; continuing without it")
            return None

    def run(self) -> None:
        self._updater.start_polling()


class MessageDelayBot(Bot):
    def __init__(self, *args, tasks_delay: float = 0.5, **kwargs):
        super(MessageDelayBot, self).__init__(*args, **kwargs)
        self._chat_tasks_lock = threading.Lock()
        self._tasks_delay = tasks_delay
        self._chat_tasks = {}
        self._stop_chat_tasks = threading.Event()
        self._chat_tasks_thread = threading.Thread(
            target=self._tasks_manager_loop, args=(self._stop_chat_tasks,)
        )
        self._mdm: Optional[MessageDeleteManager] = None

    def run_tasks_manager(self) -> None:
        self._chat_tasks_thread.start()

    def _process_chat_tasks(self) -> None:
        now = time.time()
        for chat_id, time_tasks in list(self._chat_tasks.items()):
            task_time = time_tasks.get("last_time", 0)
            tasks = time_tasks.get("tasks", [])
            if now - task_time < self._tasks_delay:
                continue
            if not tasks:
                continue
            task_callable, _ = tasks[0]
            try:
                task_callable()
                tasks.pop(0)
            except (TimedOut, NetworkError, RetryAfter) as e:
                logging.warning(f"Network error on task for chat {chat_id}: {e}. Retrying later.")
            except (BadRequest, Unauthorized, Conflict) as e:
                logging.error(f"Telegram API error for chat {chat_id}. Dropping task. Error: {e}")
                traceback.print_exc()
                tasks.pop(0)
            except Exception as e:
                logging.error(f"Unexpected error processing task for chat {chat_id}. Dropping task. Error: {e}")
                traceback.print_exc()
                tasks.pop(0)
            finally:
                if chat_id in self._chat_tasks:
                    self._chat_tasks[chat_id]["last_time"] = now

    def _tasks_manager_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            with self._chat_tasks_lock:
                try:
                    self._process_chat_tasks()
                except Exception as e:
                    logging.critical(f"FATAL ERROR in _tasks_manager_loop itself: {e}")
                    traceback.print_exc()
            time.sleep(0.1)

    def __del__(self):
        try:
            self._stop_chat_tasks.set()
            self._chat_tasks_thread.join()
        except Exception as e:
            logging.error(f"Error during MessageDelayBot destruction: {e}")

    def _add_task(self, chat_id: ChatId, task: Callable, task_type: str) -> None:
        with self._chat_tasks_lock:
            if chat_id not in self._chat_tasks:
                self._chat_tasks[chat_id] = {"last_time": 0, "tasks": []}
            self._chat_tasks[chat_id]["tasks"].append((task, task_type))

    def _sanitize_text(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return text
        return re.sub(r'\[([^\]]+)\]\(tg://user\?id=\d+\)', r'\1', text)

    def _mdm_register_safe(
        self,
        msg,
        *,
        chat_id,
        game_id=None,
        hand_id=None,
        tag="generic",
        protected=False,
        ttl=None,
    ) -> None:
        mdm = getattr(self, "_mdm", None)
        if not (mdm and msg):
            return
        for m in ("register", "add", "track"):
            if hasattr(mdm, m):
                try:
                    getattr(mdm, m)(
                        chat_id=chat_id,
                        message_id=msg.message_id,
                        game_id=game_id,
                        hand_id=hand_id,
                        tag=tag,
                        protected=protected,
                        ttl=ttl,
                    )
                except TypeError:
                    try:
                        getattr(mdm, m)(chat_id, msg.message_id, game_id, hand_id, tag, protected, ttl)
                    except Exception:
                        pass
                except Exception:
                    pass
                break

    def send_message_sync(self, *args, **kwargs):
        try:
            kwargs["text"] = self._sanitize_text(kwargs.get("text"))
            msg = super(MessageDelayBot, self).send_message(*args, **kwargs)
            self._mdm_register_safe(
                msg,
                chat_id=kwargs.get("chat_id"),
                game_id=kwargs.pop("mdm_game_id", None),
                hand_id=kwargs.pop("mdm_hand_id", None),
                tag=kwargs.pop("mdm_tag", "generic"),
                protected=kwargs.pop("mdm_protected", False),
                ttl=None,
            )
            return msg
        except Exception as e:
            logging.error(f"Error in send_message_sync: {e}")
            traceback.print_exc()
            return None

    def send_message(self, *, chat_id=None, text=None, reply_markup=None, parse_mode=None, **kwargs):
        if text is None or chat_id is None:
            return None
        text = self._sanitize_text(text)
        mdm_protected = kwargs.pop("mdm_protected", False)
        mdm_tag = kwargs.pop("mdm_tag", "generic")
        mdm_game_id = kwargs.pop("mdm_game_id", None)
        mdm_hand_id = kwargs.pop("mdm_hand_id", None)
        msg = super().send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs
        )
        self._mdm_register_safe(
            msg,
            chat_id=chat_id,
            game_id=mdm_game_id,
            hand_id=mdm_hand_id,
            tag=mdm_tag,
            protected=mdm_protected,
            ttl=None,
        )
        self._mdm_register_safe(
            msg,
            chat_id=chat_id,
            game_id=None,
            hand_id=None,
            tag="chat_buffer",
            protected=mdm_protected,
            ttl=None,
        )
        return msg

    def send_photo(self, *args, **kwargs) -> None:
        chat_id = kwargs.get("chat_id", 0)
        task = lambda: super(MessageDelayBot, self).send_photo(*args, **kwargs)
        self._add_task(chat_id, task, "send")

    def edit_message_reply_markup(self, *args, **kwargs) -> None:
        try:
            super(MessageDelayBot, self).edit_message_reply_markup(*args, **kwargs)
        except (BadRequest, Conflict) as e:
            logging.info(f"Could not edit reply markup: {e}")
        except (TimedOut, NetworkError, RetryAfter) as e:
            logging.warning(f"Network error on edit_message_reply_markup: {e}")
        except Exception as e:
            logging.error(f"Unexpected error on edit_message_reply_markup: {e}")
            traceback.print_exc()
