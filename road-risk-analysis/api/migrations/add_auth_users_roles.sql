-- Module 1 (security & users): users, roles, permission matrix, sessions,
-- password-reset tokens, API clients, audit trail and runtime settings.
-- The API creates these automatically on startup (Base.metadata.create_all);
-- this file documents the schema and can be applied by hand.

CREATE TYPE public.language AS ENUM ('en', 'fr', 'es');
CREATE TYPE public.mapprovider AS ENUM ('default', 'osm', 'google');
CREATE TYPE public.sessionstage AS ENUM ('full', 'mfa', 'mfa_setup');
CREATE TYPE public.userstatus AS ENUM ('active', 'inactive');

CREATE TABLE public.api_clients (
    id character varying NOT NULL,
    name character varying NOT NULL,
    description character varying,
    key_prefix character varying NOT NULL,
    key_hash character varying NOT NULL,
    is_active boolean NOT NULL,
    created_by character varying,
    created_at timestamp without time zone NOT NULL,
    rotated_at timestamp without time zone,
    revoked_at timestamp without time zone,
    last_used_at timestamp without time zone
);

CREATE TABLE public.audit_logs (
    id character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    actor_type character varying NOT NULL,
    actor_id character varying,
    actor_label character varying,
    action character varying NOT NULL,
    outcome character varying NOT NULL,
    object_type character varying,
    object_id character varying,
    details json,
    ip character varying,
    user_agent character varying
);

CREATE TABLE public.auth_sessions (
    id character varying NOT NULL,
    user_id character varying NOT NULL,
    token_hash character varying NOT NULL,
    stage public.sessionstage NOT NULL,
    created_at timestamp without time zone NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    last_used_at timestamp without time zone,
    revoked_at timestamp without time zone,
    ip character varying,
    user_agent character varying
);

CREATE TABLE public.password_reset_tokens (
    id character varying NOT NULL,
    user_id character varying NOT NULL,
    token_hash character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    used_at timestamp without time zone
);

CREATE TABLE public.role_permissions (
    id character varying NOT NULL,
    role_id character varying NOT NULL,
    module character varying NOT NULL,
    can_view boolean NOT NULL,
    can_add boolean NOT NULL,
    can_edit boolean NOT NULL,
    can_delete boolean NOT NULL
);

CREATE TABLE public.roles (
    id character varying NOT NULL,
    name character varying NOT NULL,
    description character varying,
    is_active boolean NOT NULL,
    is_system boolean NOT NULL,
    mfa_required boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE public.system_settings (
    section character varying NOT NULL,
    value json NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    updated_by character varying
);

CREATE TABLE public.user_roles (
    user_id character varying NOT NULL,
    role_id character varying NOT NULL
);

CREATE TABLE public.users (
    id character varying NOT NULL,
    email character varying NOT NULL,
    username character varying,
    full_name character varying NOT NULL,
    phone character varying,
    whatsapp character varying,
    status public.userstatus NOT NULL,
    password_hash character varying,
    password_changed_at timestamp without time zone,
    map_provider public.mapprovider NOT NULL,
    language public.language NOT NULL,
    mfa_enabled boolean NOT NULL,
    mfa_secret_enc character varying,
    mfa_pending_enc character varying,
    mfa_last_step integer,
    failed_login_count integer NOT NULL,
    locked_until timestamp without time zone,
    last_login_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

ALTER TABLE ONLY public.api_clients
    ADD CONSTRAINT api_clients_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_name_key UNIQUE (name);

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.system_settings
    ADD CONSTRAINT system_settings_pkey PRIMARY KEY (section);

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT uq_role_permission_module UNIQUE (role_id, module);

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_pkey PRIMARY KEY (user_id, role_id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);

CREATE UNIQUE INDEX ix_api_clients_key_hash ON public.api_clients USING btree (key_hash);

CREATE INDEX ix_audit_logs_action ON public.audit_logs USING btree (action);

CREATE INDEX ix_audit_logs_actor_id ON public.audit_logs USING btree (actor_id);

CREATE INDEX ix_audit_logs_created_at ON public.audit_logs USING btree (created_at);

CREATE INDEX ix_audit_logs_object ON public.audit_logs USING btree (object_type, object_id);

CREATE UNIQUE INDEX ix_auth_sessions_token_hash ON public.auth_sessions USING btree (token_hash);

CREATE INDEX ix_auth_sessions_user_id ON public.auth_sessions USING btree (user_id);

CREATE UNIQUE INDEX ix_password_reset_tokens_token_hash ON public.password_reset_tokens USING btree (token_hash);

CREATE INDEX ix_password_reset_tokens_user_id ON public.password_reset_tokens USING btree (user_id);

CREATE INDEX ix_role_permissions_role_id ON public.role_permissions USING btree (role_id);

CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);

CREATE UNIQUE INDEX ix_users_username ON public.users USING btree (username);

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


