#!/usr/bin/env python3

import logging
import threading
import time
import redis
import traceback
import re

from typing import Callable, Optional
from telegram import Bot
from telegram.utils.request import Request
from telegram.ext import Updater
from telegram.error import (
    TimedOut,
    NetworkError,
    RetryAfter,
    BadRequest,
    ChatMigrated,
    Conflict,
    InvalidToken,
    TelegramError,
    Unauthorized,
)

from pokerapp.config import Config
from pokerapp.pokerbotcontrol import PokerBotCotroller
from pokerapp.pokerbotmodel import PokerBotModel
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.entities import ChatId
from pokerapp.message_delete_manager import MessageDeleteManager


class PokerBot:
    def __init__(self, token, mdm: Optional[MessageDeleteManager] = None, **kw):
        cfg = Config()
        req = Request(con_pool_size=8)
        bot = MessageDelayBot(token=token, request=req)
        bot.run_tasks_manager()

        self._updater = Updater(bot=bot, use_context=True)

        kv = redis.Redis(
            host=cfg.REDIS_HOST,
            port=cfg.REDIS_PORT,
            db=cfg.REDIS_DB,
            password=cfg.REDIS_PASS if cfg.REDIS_PASS != "" else None
        )

        self._mdm = mdm or MessageDeleteManager(
            bot=bot,
            job_queue=self._updater.job_queue,
            default_ttl=getattr(cfg, "DEFAULT_DELETE_TTL_SECONDS", None),
        )
        bot._mdm = self._mdm

        self._view = PokerBotViewer(bot=bot, mdm=self._mdm, cfg=cfg)
        self._model = PokerBotModel(view=self._view, bot=bot, kv=kv, cfg=cfg, mdm=self._mdm)
        self._controller = PokerBotCotroller(self._model, self._updater, mdm=self._mdm)

    def run(self) -> None:
        self._updater.start_polling()


class MessageDelayBot(Bot):
    def __init__(self, *args, tasks_delay=0.5, **kwargs):
        super(MessageDelayBot, self).__init__(*args, **kwargs)
        self._chat_tasks_lock = threading.Lock()
        self._tasks_delay = tasks_delay
        self._chat_tasks = {}
        self._stop_chat_tasks = threading.Event()
        self._chat_tasks_thread = threading.Thread(
            target=self._tasks_manager_loop,
            args=(self._stop_chat_tasks,),
            daemon=True,
        )

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
            task_callable, task_type = tasks[0]
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

    def send_message_sync(self, *args, **kwargs):
        try:
            return super(MessageDelayBot, self).send_message(*args, **kwargs)
        except Exception as e:
            logging.error(f"Error in send_message_sync: {e}")
            traceback.print_exc()
            return None

    def __del__(self):
        try:
            self._stop_chat_tasks.set()
            if self._chat_tasks_thread.is_alive():
                self._chat_tasks_thread.join(timeout=0.5)
        except Exception as e:
            logging.error(f"Error during MessageDelayBot destruction: {e}")

    def _add_task(self, chat_id: ChatId, task: Callable, task_type: str) -> None:
        with self._chat_tasks_lock:
            if chat_id not in self._chat_tasks:
                self._chat_tasks[chat_id] = {"last_time": 0, "tasks": []}
            self._chat_tasks[chat_id]["tasks"].append((task, task_type))

    def _sanitize_text(self, text: str) -> str:
        if not text:
            return text
        return re.sub(r'\[([^\]]+)\]\(tg://user\?id=\d+\)', r'\1', text)

    def _mdm_register_safe(self, msg, *, chat_id, mdm_game_id, mdm_hand_id, mdm_tag, mdm_protected):
        mdm = getattr(self, "_mdm", None)
        if not (mdm and msg):
            return
        try:
            if hasattr(mdm, "register_message"):
                mdm.register_message(chat_id=chat_id, message_id=msg.message_id, game_id=mdm_game_id, hand_id=mdm_hand_id, tag=mdm_tag, protected=mdm_protected, ttl=None)
            elif hasattr(mdm, "add_message"):
                mdm.add_message(chat_id=chat_id, message_id=msg.message_id, game_id=mdm_game_id, hand_id=mdm_hand_id, tag=mdm_tag, protected=mdm_protected, ttl=None)
            elif hasattr(mdm, "track_message"):
                mdm.track_message(chat_id=chat_id, message_id=msg.message_id, game_id=mdm_game_id, hand_id=mdm_hand_id, tag=mdm_tag, protected=mdm_protected, ttl=None)
            elif hasattr(mdm, "add"):
                mdm.add(chat_id=chat_id, message_id=msg.message_id, game_id=mdm_game_id, hand_id=mdm_hand_id, tag=mdm_tag, protected=mdm_protected, ttl=None)
            else:
                logging.info("MDM has no known register method; skipping.")
        except Exception as e:
            logging.info(f"MDM register failed: {e}")

        try:
            if hasattr(mdm, "register_message"):
                mdm.register_message(chat_id=chat_id, message_id=msg.message_id, game_id=None, hand_id=None, tag="chat_buffer", protected=mdm_protected, ttl=None)
            elif hasattr(mdm, "add_message"):
                mdm.add_message(chat_id=chat_id, message_id=msg.message_id, game_id=None, hand_id=None, tag="chat_buffer", protected=mdm_protected, ttl=None)
            elif hasattr(mdm, "track_message"):
                mdm.track_message(chat_id=chat_id, message_id=msg.message_id, game_id=None, hand_id=None, tag="chat_buffer", protected=mdm_protected, ttl=None)
            elif hasattr(mdm, "add"):
                mdm.add(chat_id=chat_id, message_id=msg.message_id, game_id=None, hand_id=None, tag="chat_buffer", protected=mdm_protected, ttl=None)
        except Exception as e:
            logging.info(f"MDM register(chat_buffer) failed: {e}")

    def send_message(self, *, chat_id=None, text=None, reply_markup=None, parse_mode=None, **kwargs):
        if text is None or chat_id is None:
            return None
        mdm_protected = kwargs.pop("mdm_protected", False)
        mdm_tag = kwargs.pop("mdm_tag", "generic")
        mdm_game_id = kwargs.pop("mdm_game_id", None)
        mdm_hand_id = kwargs.pop("mdm_hand_id", None)
        text = self._sanitize_text(text)
        msg = super().send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
        self._mdm_register_safe(msg, chat_id=chat_id, mdm_game_id=mdm_game_id, mdm_hand_id=mdm_hand_id, mdm_tag=mdm_tag, mdm_protected=mdm_protected)
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
