CREATE TABLE feature_message (
	owner_id VARCHAR NOT NULL,
	event_key VARCHAR NOT NULL,
	agent_id BIGINT NOT NULL,
	project VARCHAR NOT NULL,
	text TEXT NOT NULL,
	result JSON NOT NULL,
	PRIMARY KEY (owner_id, event_key)
);

CREATE TABLE knowledge_weekly_grant (
	owner_id VARCHAR NOT NULL,
	employee_id BIGINT NOT NULL,
	source_job_id BIGINT NOT NULL,
	task_id BIGINT NOT NULL,
	assignment_epoch INTEGER NOT NULL,
	scope JSON NOT NULL,
	enabled BOOLEAN NOT NULL,
	generation VARCHAR(32) NOT NULL,
	next_due TIMESTAMP WITH TIME ZONE NOT NULL,
	blocked_reason VARCHAR(300),
	PRIMARY KEY (owner_id, employee_id)
);

CREATE TABLE knowledge_weekly_slot (
	event_key VARCHAR(90) NOT NULL,
	owner_id VARCHAR NOT NULL,
	employee_id BIGINT NOT NULL,
	job_id BIGINT NOT NULL,
	scope_digest VARCHAR(64) NOT NULL,
	cutoff BIGINT NOT NULL,
	since BIGINT,
	PRIMARY KEY (event_key),
	UNIQUE (job_id)
);

CREATE TABLE steward_direct_turn (
	owner_id VARCHAR NOT NULL,
	event_key VARCHAR NOT NULL,
	thread_id BIGINT NOT NULL,
	turn_id BIGINT NOT NULL,
	input_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (owner_id, event_key)
);

CREATE TABLE feishu_lead_chat (
	thread_id BIGINT NOT NULL,
	channel VARCHAR(64) NOT NULL,
	selection VARCHAR(64) NOT NULL,
	model VARCHAR,
	project VARCHAR NOT NULL,
	isolated BOOLEAN NOT NULL,
	PRIMARY KEY (thread_id),
	CONSTRAINT uq_feishu_lead_selection UNIQUE (channel, selection),
	FOREIGN KEY(thread_id) REFERENCES thread (id) ON DELETE CASCADE
);

CREATE TABLE feature_scope (
	task_id BIGINT NOT NULL,
	assignment_epoch INTEGER DEFAULT '1' NOT NULL,
	project VARCHAR NOT NULL,
	version VARCHAR NOT NULL,
	acceptance TEXT NOT NULL,
	aliases JSON NOT NULL,
	dependencies JSON,
	deliverables JSON,
	checkpoint TEXT NOT NULL,
	related_id BIGINT,
	PRIMARY KEY (task_id),
	FOREIGN KEY(task_id) REFERENCES work_item (id),
	FOREIGN KEY(related_id) REFERENCES work_item (id)
);

CREATE TABLE feature_session (
	thread_id BIGINT NOT NULL,
	task_id BIGINT NOT NULL,
	assignment_epoch INTEGER DEFAULT '1' NOT NULL,
	previous_id BIGINT,
	PRIMARY KEY (thread_id),
	FOREIGN KEY(thread_id) REFERENCES thread (id),
	FOREIGN KEY(task_id) REFERENCES work_item (id),
	FOREIGN KEY(previous_id) REFERENCES thread (id)
);

CREATE INDEX ix_feature_session_task_id ON feature_session (task_id);

CREATE TABLE steward_suggestion (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	owner_id VARCHAR NOT NULL,
	task_id BIGINT NOT NULL,
	source_key VARCHAR(64) NOT NULL,
	kind VARCHAR NOT NULL,
	title TEXT NOT NULL,
	detail TEXT NOT NULL,
	state VARCHAR NOT NULL,
	revision INTEGER NOT NULL,
	option VARCHAR,
	report TEXT,
	followup_task_id BIGINT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (owner_id, task_id, source_key),
	FOREIGN KEY(task_id) REFERENCES work_item (id) ON DELETE CASCADE
);

CREATE INDEX ix_steward_suggestion_inbox ON steward_suggestion (owner_id, state, id);
