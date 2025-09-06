#!/usr/bin/env python3

import logging
import threading
import time
import redis
import traceback  # <--- اضافه شد برای لاگ دقیق‌تر

from typing import Callable
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
    def __init__(
        self,
        token: str,
        cfg: Config,
    ):
        req = Request(con_pool_size=8)
        bot = MessageDelayBot(token=token, request=req)
        bot.run_tasks_manager()

        self._updater = Updater(
            bot=bot,
            use_context=True,
        )

        kv = redis.Redis(
            host=cfg.REDIS_HOST,
            port=cfg.REDIS_PORT,
            db=cfg.REDIS_DB,
            password=cfg.REDIS_PASS if cfg.REDIS_PASS != "" else None
        )
        # MessageDeleteManager: مدیریت متمرکز حذف پیام‌ها
        self._mdm = MessageDeleteManager(
            bot=bot,
            job_queue=self._updater.job_queue,
            default_ttl=getattr(cfg, "DEFAULT_DELETE_TTL_SECONDS", None),
        )

        self._view = PokerBotViewer(bot=bot, mdm=self._mdm, cfg=cfg)  # ← mdm و cfg به View
        self._model = PokerBotModel(
            view=self._view,
            bot=bot,
            kv=kv,
            cfg=cfg,
            mdm=self._mdm,  # ← عبور mdm به Model
        )
        self._controller = PokerBotCotroller(self._model, self._updater, mdm=self._mdm)  # ← mdm به Controller

    def run(self) -> None:
        self._updater.start_polling()

class MessageDelayBot(Bot):
    def __init__(
        self,
        *args,
        tasks_delay=0.5,
        **kwargs,
    ):
        super(MessageDelayBot, self).__init__(*args, **kwargs)

        self._chat_tasks_lock = threading.Lock()
        self._tasks_delay = tasks_delay
        self._chat_tasks = {}
        self._stop_chat_tasks = threading.Event()
        self._chat_tasks_thread = threading.Thread(
            target=self._tasks_manager_loop,
            args=(self._stop_chat_tasks, ),
        )

    def run_tasks_manager(self) -> None:
        self._chat_tasks_thread.start()

    def _process_chat_tasks(self) -> None:
        now = time.time()

        # Iterate over a copy of items to allow modification during iteration
        for chat_id, time_tasks in list(self._chat_tasks.items()):
            task_time = time_tasks.get("last_time", 0)
            tasks = time_tasks.get("tasks", [])

            if now - task_time < self._tasks_delay:
                continue

            if not tasks:
                continue

            # Get the next task without removing it yet
            task_callable, task_type = tasks[0]

            try:
                task_callable()
                # If successful, remove the task from the queue
                tasks.pop(0)
            except (TimedOut, NetworkError, RetryAfter) as e:
                # If it's a network error, log it and keep it in the queue for the next try.
                logging.warning(f"Network error on task for chat {chat_id}: {e}. Retrying later.")
                # We don't remove it, so it will be retried.
            except (BadRequest, Unauthorized, Conflict) as e:
                # These are non-recoverable errors for this specific task.
                # Log the error and remove the task to prevent infinite loops.
                logging.error(f"Telegram API error for chat {chat_id}. Dropping task. Error: {e}")
                traceback.print_exc()
                tasks.pop(0)  # Remove the faulty task
            except Exception as e:
                # Catch any other unexpected errors
                logging.error(f"Unexpected error processing task for chat {chat_id}. Dropping task. Error: {e}")
                traceback.print_exc()
                tasks.pop(0) # Remove the faulty task
            finally:
                # Update the last execution time for this chat_id
                if chat_id in self._chat_tasks:
                     self._chat_tasks[chat_id]["last_time"] = now

    def _tasks_manager_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            with self._chat_tasks_lock:
                try:
                    self._process_chat_tasks()
                except Exception as e:
                    # Prevent the manager loop from crashing
                    logging.critical(f"FATAL ERROR in _tasks_manager_loop itself: {e}")
                    traceback.print_exc()
            time.sleep(0.1) # A slightly longer sleep can be better
            
    def send_message_sync(self, *args, **kwargs):
        """
        Sends a message immediately, bypassing the queue.
        This should be used for critical messages that need a return value.
        """
        try:
            # فراخوانی مستقیم متد از کلاس پدر (Bot)
            return super(MessageDelayBot, self).send_message(*args, **kwargs)
        except Exception as e:
            logging.error(f"Error in send_message_sync: {e}")
            traceback.print_exc()
            return None

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
            # Append to the end, process from the beginning (FIFO)
            self._chat_tasks[chat_id]["tasks"].append((task, task_type))

    # ==================== متدهای اصلاح شده ====================

    def send_message(self, *args, **kwargs) -> None:
        # Ensure text is present before adding the task
        if 'text' not in kwargs or kwargs['text'] is None:
            logging.error("send_message called without 'text'. Ignoring.")
            traceback.print_stack() # Print stack to find the caller
            return

        chat_id = kwargs.get("chat_id", 0)
        task = lambda: super(MessageDelayBot, self).send_message(*args, **kwargs)
        self._add_task(chat_id, task, "send")

    def send_photo(self, *args, **kwargs) -> None:
        chat_id = kwargs.get("chat_id", 0)
        task = lambda: super(MessageDelayBot, self).send_photo(*args, **kwargs)
        self._add_task(chat_id, task, "send")

    def edit_message_reply_markup(self, *args, **kwargs) -> None:
        # This function should not be queued like send_message.
        # It's for immediate interaction feedback.
        # Queuing it can lead to editing a message that no longer exists or is irrelevant.
        # We will try to execute it immediately.
        try:
            super(MessageDelayBot, self).edit_message_reply_markup(*args, **kwargs)
        except (BadRequest, Conflict) as e:
            # Common errors: message not found, or message not modified.
            # These are safe to ignore in most cases (e.g., trying to remove markup twice).
            logging.info(f"Could not edit reply markup: {e}")
        except (TimedOut, NetworkError, RetryAfter) as e:
            # If there's a network issue, we can log it. Retrying is complex for edits.
            logging.warning(f"Network error on edit_message_reply_markup: {e}")
        except Exception as e:
            # Catch other potential errors
            logging.error(f"Unexpected error on edit_message_reply_markup: {e}")
            traceback.print_exc()
