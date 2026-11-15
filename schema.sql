CREATE TABLE IF NOT EXISTS programs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  slug TEXT NOT NULL,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  bounty_min REAL,
  bounty_max REAL,
  reward_raw TEXT,
  tags TEXT DEFAULT '[]',
  languages TEXT DEFAULT '[]',
  state TEXT DEFAULT 'active',
  end_date TEXT,
  seed INTEGER DEFAULT 0,
  logo_url TEXT,
  focus TEXT DEFAULT '',
  blurb TEXT DEFAULT '',
  last_deep TEXT DEFAULT '',
  upstream_listed TEXT DEFAULT '',
  upstream_updated TEXT DEFAULT '',
  vdp INTEGER DEFAULT 0,
  is_contest INTEGER DEFAULT 0,
  report_count INTEGER DEFAULT 0,
  first_seen TEXT,
  last_seen TEXT,
  UNIQUE(platform, slug)
);
CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  program_id INTEGER NOT NULL REFERENCES programs(id),
  identity TEXT NOT NULL,
  eligible INTEGER DEFAULT 1,
  in_scope INTEGER DEFAULT 1,
  target_type TEXT DEFAULT 'other',
  first_seen TEXT,
  last_seen TEXT,
  UNIQUE(program_id, identity)
);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  program_id INTEGER REFERENCES programs(id),
  detail TEXT DEFAULT '',
  seen INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scrapes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  ts TEXT NOT NULL,
  count INTEGER DEFAULT 0,
  status TEXT DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_changes_ts ON changes(ts DESC);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  program_id INTEGER NOT NULL REFERENCES programs(id),
  identity TEXT NOT NULL,
  ts TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  summary TEXT DEFAULT '',
  raw TEXT DEFAULT '',
  mode TEXT DEFAULT 'light',
  started TEXT DEFAULT '',
  findings INTEGER DEFAULT 0,
  findings_json TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scans_prog ON scans(program_id);
CREATE TABLE IF NOT EXISTS cves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  program_id INTEGER NOT NULL REFERENCES programs(id),
  identity TEXT NOT NULL,
  cve_id TEXT NOT NULL,
  ghsa_id TEXT DEFAULT '',
  url TEXT NOT NULL,
  summary TEXT DEFAULT '',
  severity TEXT DEFAULT '',
  score REAL,
  published TEXT DEFAULT '',
  source TEXT DEFAULT 'crv-leak-scan',
  ts TEXT NOT NULL,
  UNIQUE(program_id, identity, cve_id)
);
CREATE INDEX IF NOT EXISTS idx_cves_prog ON cves(program_id);
CREATE INDEX IF NOT EXISTS idx_programs_platform ON programs(platform);
