import asyncio
import datetime
from typing import Dict
from pyee import AsyncIOEventEmitter
from services.data.postgres_async_db import AsyncPostgresDB
from .notify import resource_list

HEARTBEAT_INTERVAL = 10 # interval of heartbeats, in seconds

class RunHeartbeatMonitor(object):
  '''
  Service class for adding objects with heartbeat timestamps to be monitored and acted upon
  when heartbeat becomes too old.

  Starts an async watcher loop that periodically checks the heartbeat of the subscribed runs for expired timestamps

  Usage
  -----
  Responds to event_emitter emissions with messages:
    "run-heartbeat", "update", run_id -> updates heartbeat timestamp that is found in database
    "run-heartbeat", "complete" run_id -> removes run from heartbeat checks
  '''
  def __init__(self, event_emitter=None, key = "run_number", heartbeat_field = "last_heartbeat_ts"):
    self.heartbeat_field = heartbeat_field
    self.key = key
    self.watched = {}
    self._run_table = AsyncPostgresDB.get_instance().run_table_postgres
    # Handle DB Events
    self.event_emitter = event_emitter or AsyncIOEventEmitter()
    event_emitter.on('run-heartbeat', self.heartbeat_handler)

    # Start heartbeat watcher
    loop = asyncio.get_event_loop()
    loop.create_task(self.check_heartbeats())

  async def heartbeat_handler(self, action, run_id):
    if action == "update":
      await self.add_run_to_watch(run_id)

    if action == "complete":
      self.remove_run_from_watch(run_id)
    
  async def add_run_to_watch(self, run_id):
    run = await self.get_run(run_id)

    if self.heartbeat_field in run and self.key in run:
      run_number = run[self.key]
      heartbeat_ts = run[self.heartbeat_field]
      if heartbeat_ts is not None: # only start monitoring on runs that have a heartbeat
        self.watched[run_number] = heartbeat_ts

  def remove_run_from_watch(self, run_id):
    self.watched.pop(run_id, None)

  async def get_run(self, run_id):
    # Remember to enable_joins for the query, otherwise the 'status' will be missing from the run
    # and we can not broadcast an up-to-date status.
    result, _ = await self._run_table.find_records(
                            conditions=["run_number = %s"],
                            values=[run_id],
                            fetch_single=True,
                            enable_joins=True
                          )
    return result.body if result.response_code==200 else None
  
  async def load_and_broadcast(self, run_id):
    '''
    Load updated run and broadcast the changes to the websocket event handler.
    '''
    run = await self.get_run(run_id)
    resources = resource_list(self._run_table.table_name, run)
    self.event_emitter.emit('notify', 'UPDATE', resources, run)

  async def check_heartbeats(self):
    '''
    Async Task that is responsible for checking the heartbeats of all monitored runs,
    and triggering handlers in case the heartbeat is too old.
    '''
    while True:
      time_now = int(datetime.datetime.utcnow().timestamp()) # same format as the metadata heartbeat uses
      # print(f"checking heartbeats of {len(self.watched)} runs, timestamp: {time_now}", flush=True)
      for run_number, last_heartbeat_ts in list(self.watched.items()):
        if time_now - last_heartbeat_ts > HEARTBEAT_INTERVAL * 2:
          await self.load_and_broadcast(run_number)
          self.remove_run_from_watch(run_number)
          
      await asyncio.sleep(HEARTBEAT_INTERVAL)
