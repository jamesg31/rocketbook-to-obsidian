DROP TABLE IF EXISTS email;

CREATE TABLE email (
  message_id TEXT PRIMARY KEY,
  processed BOOLEAN
);
