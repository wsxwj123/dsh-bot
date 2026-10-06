-- 送达账本的表结构。网关（gateway/src/ledger.ts）和 Python 周边的测试共用这一份。
-- Python 周边脚本只读这个库（chat_history.py 等）。
-- 改表结构时：这里只加不删，老库由 Ledger.migrate() 补列。
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS inbound (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ukey TEXT NOT NULL UNIQUE,
  chat_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  tg_message_id INTEGER,
  sender_id TEXT,
  sender_name TEXT,
  text TEXT NOT NULL,
  meta TEXT,
  ts INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  turn_id INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  note TEXT
);
CREATE INDEX IF NOT EXISTS inbound_chat_state ON inbound(chat_id, state, id);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  session_id TEXT,
  mcp_token TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  closed_at INTEGER,
  last_used_at INTEGER,
  used_tokens INTEGER,
  window INTEGER,
  model TEXT,
  needs_seed INTEGER NOT NULL DEFAULT 0,
  summary TEXT,
  close_reason TEXT,
  memory_seen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS segments_chat ON segments(chat_id, state);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_id INTEGER NOT NULL,
  chat_id TEXT NOT NULL,
  segment_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  inbound_ids TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  sent_at INTEGER,
  ended_at INTEGER,
  stop_reason TEXT,
  error TEXT,
  silent INTEGER NOT NULL DEFAULT 0,
  silent_reason TEXT,
  used_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS turns_state ON turns(state);
CREATE TABLE IF NOT EXISTS outbound (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  okey TEXT UNIQUE,
  chat_id TEXT NOT NULL,
  turn_id INTEGER,
  part INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL,
  text TEXT,
  file TEXT,
  reply_to INTEGER,
  of_parts INTEGER,
  state TEXT NOT NULL,
  tg_message_id INTEGER,
  error TEXT,
  created_at INTEGER NOT NULL,
  sent_at INTEGER
);
CREATE INDEX IF NOT EXISTS outbound_turn ON outbound(turn_id);
CREATE INDEX IF NOT EXISTS outbound_chat ON outbound(chat_id, id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL,
  chat_id TEXT,
  kind TEXT NOT NULL,
  data TEXT
);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL,
  chat_id TEXT,
  text TEXT NOT NULL
);
