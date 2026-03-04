from django.core.management.base import BaseCommand
from django.db import connection


DDL = """
BEGIN;

ALTER TABLE guests
  DROP COLUMN IF EXISTS is_stop_sending;

CREATE TABLE IF NOT EXISTS message_templates (
  id bigserial PRIMARY KEY,
  name varchar(150) NOT NULL,
  description varchar(255),
  message_text text NOT NULL,
  created_by varchar(100) NOT NULL DEFAULT 'test_user',
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mailings (
  id bigserial PRIMARY KEY,
  name varchar(150) NOT NULL,
  template_id bigint NOT NULL REFERENCES message_templates(id) ON DELETE RESTRICT,
  scheduled_date date NOT NULL,
  scheduled_time_begin timestamptz NOT NULL,
  scheduled_time_end timestamptz NOT NULL,
  is_active boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  send_window_begin time NOT NULL,
  send_window_end time NOT NULL
);

CREATE TABLE IF NOT EXISTS mailing_channels (
  id bigserial PRIMARY KEY,
  name varchar(150) NOT NULL,
  channel_kind varchar(50) NOT NULL,
  token text,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mailing_channel_links (
  id bigserial PRIMARY KEY,
  mailing_id bigint NOT NULL REFERENCES mailings(id) ON DELETE CASCADE,
  channel_id bigint NOT NULL REFERENCES mailing_channels(id) ON DELETE RESTRICT,
  UNIQUE (mailing_id, channel_id)
);

CREATE TABLE IF NOT EXISTS mailing_guests (
  id bigserial PRIMARY KEY,
  mailing_id bigint NOT NULL REFERENCES mailings(id) ON DELETE CASCADE,
  guest_id bigint NOT NULL REFERENCES guests(id) ON DELETE CASCADE,
  phone varchar(50),
  email varchar(150),
  text_mailing_list text NOT NULL,
  scheduled_datetime timestamptz NOT NULL,
  status varchar(20) NOT NULL DEFAULT 'planned',
  error_description text,
  external_id varchar(32),
  sent_at timestamptz,
  delivery_status varchar(50),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (mailing_id, guest_id)
);

CREATE TABLE IF NOT EXISTS guest_channel_links (
  id bigserial PRIMARY KEY,
  guest_id bigint NOT NULL REFERENCES guests(id) ON DELETE CASCADE,
  channel_id bigint NOT NULL REFERENCES mailing_channels(id) ON DELETE CASCADE,
  external_chat_id varchar(64),
  is_opt_in boolean NOT NULL DEFAULT true,
  is_active boolean NOT NULL DEFAULT true,
  is_stop_sending boolean NOT NULL DEFAULT false,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (guest_id, channel_id)
);

COMMIT;
"""


class Command(BaseCommand):
    help = "Initialize mailing schema"

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute(DDL)

        self.stdout.write(self.style.SUCCESS("Schema initialized successfully"))