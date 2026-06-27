--
-- PostgreSQL database dump
--

\restrict UAIbi5vKdkhsbpbjBoZkJIapfe5j0nor6r0HVFh3NixCvzk1MsnJ1D3kwKLQvrU

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: appearance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.appearance (
    id bigint NOT NULL,
    group_id bigint,
    cam_id integer,
    ts timestamp with time zone NOT NULL,
    body_vector public.vector(512) NOT NULL,
    face_vector public.vector(512),
    track_id text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: appearance_crop; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.appearance_crop (
    id bigint NOT NULL,
    appearance_id bigint,
    kind text NOT NULL,
    path text NOT NULL,
    frame_idx integer,
    quality real,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT appearance_crop_kind_check CHECK ((kind = ANY (ARRAY['body'::text, 'face'::text])))
);


--
-- Name: appearance_crop_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.appearance_crop_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: appearance_crop_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.appearance_crop_id_seq OWNED BY public.appearance_crop.id;


--
-- Name: appearance_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.appearance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: appearance_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.appearance_id_seq OWNED BY public.appearance.id;


--
-- Name: cameras; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cameras (
    id integer NOT NULL,
    cam_uid text NOT NULL,
    name text NOT NULL,
    rtsp_url text NOT NULL,
    mjpeg_url text,
    vendor text DEFAULT 'axis'::text,
    model text,
    location text,
    enabled boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    counting_enabled boolean DEFAULT false NOT NULL,
    fall_detection_enabled boolean DEFAULT false NOT NULL,
    reid_enabled boolean DEFAULT false NOT NULL,
    live_enabled boolean DEFAULT false NOT NULL
);


--
-- Name: cameras_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.cameras_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: cameras_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.cameras_id_seq OWNED BY public.cameras.id;


--
-- Name: events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.events (
    id bigint NOT NULL,
    cam_id integer,
    ts timestamp with time zone NOT NULL,
    type text NOT NULL,
    direction text,
    axis_object_id text,
    payload jsonb NOT NULL,
    snapshot_path text,
    face_path text,
    face_score real,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.events_id_seq OWNED BY public.events.id;


--
-- Name: incidents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.incidents (
    id bigint NOT NULL,
    "time" text NOT NULL,
    time_local text,
    status text NOT NULL,
    camera text,
    confidence real,
    ai_result text,
    ai_raw text,
    ai_response text,
    message text,
    error text,
    image_file text,
    teldrive_image_id text,
    teldrive_image_name text,
    teldrive_image_path text,
    teldrive_video_id text,
    teldrive_video_name text,
    teldrive_video_path text
);


--
-- Name: incidents_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.incidents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: incidents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.incidents_id_seq OWNED BY public.incidents.id;


--
-- Name: person_group; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.person_group (
    id bigint NOT NULL,
    cam_id integer,
    first_seen timestamp with time zone NOT NULL,
    last_seen timestamp with time zone NOT NULL,
    visit_count integer DEFAULT 1 NOT NULL,
    rep_body_vector public.vector(512) NOT NULL,
    rep_face_vector public.vector(512),
    rep_crop_path text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: person_group_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.person_group_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: person_group_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.person_group_id_seq OWNED BY public.person_group.id;


--
-- Name: settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.settings (
    key text NOT NULL,
    value text NOT NULL,
    updated_at text NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id bigint NOT NULL,
    username text NOT NULL,
    password_hash text NOT NULL,
    created_at text NOT NULL
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: appearance id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.appearance ALTER COLUMN id SET DEFAULT nextval('public.appearance_id_seq'::regclass);


--
-- Name: appearance_crop id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.appearance_crop ALTER COLUMN id SET DEFAULT nextval('public.appearance_crop_id_seq'::regclass);


--
-- Name: cameras id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cameras ALTER COLUMN id SET DEFAULT nextval('public.cameras_id_seq'::regclass);


--
-- Name: events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events ALTER COLUMN id SET DEFAULT nextval('public.events_id_seq'::regclass);


--
-- Name: incidents id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.incidents ALTER COLUMN id SET DEFAULT nextval('public.incidents_id_seq'::regclass);


--
-- Name: person_group id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.person_group ALTER COLUMN id SET DEFAULT nextval('public.person_group_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: appearance_crop appearance_crop_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.appearance_crop
    ADD CONSTRAINT appearance_crop_pkey PRIMARY KEY (id);


--
-- Name: appearance appearance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.appearance
    ADD CONSTRAINT appearance_pkey PRIMARY KEY (id);


--
-- Name: cameras cameras_cam_uid_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cameras
    ADD CONSTRAINT cameras_cam_uid_key UNIQUE (cam_uid);


--
-- Name: cameras cameras_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cameras
    ADD CONSTRAINT cameras_pkey PRIMARY KEY (id);


--
-- Name: events events_cam_id_axis_object_id_ts_direction_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_cam_id_axis_object_id_ts_direction_key UNIQUE (cam_id, axis_object_id, ts, direction);


--
-- Name: events events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);


--
-- Name: incidents incidents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.incidents
    ADD CONSTRAINT incidents_pkey PRIMARY KEY (id);


--
-- Name: person_group person_group_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.person_group
    ADD CONSTRAINT person_group_pkey PRIMARY KEY (id);


--
-- Name: settings settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settings
    ADD CONSTRAINT settings_pkey PRIMARY KEY (key);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: appearance_crop_app; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX appearance_crop_app ON public.appearance_crop USING btree (appearance_id);


--
-- Name: appearance_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX appearance_group ON public.appearance USING btree (group_id, ts DESC);


--
-- Name: cameras_counting; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cameras_counting ON public.cameras USING btree (counting_enabled) WHERE (enabled = true);


--
-- Name: cameras_fall_det; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cameras_fall_det ON public.cameras USING btree (fall_detection_enabled) WHERE (enabled = true);


--
-- Name: events_cam_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_cam_ts ON public.events USING btree (cam_id, ts DESC);


--
-- Name: events_type_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_type_ts ON public.events USING btree (type, ts DESC);


--
-- Name: idx_incidents_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_incidents_time ON public.incidents USING btree ("time" DESC);


--
-- Name: person_group_body_ivf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX person_group_body_ivf ON public.person_group USING ivfflat (rep_body_vector public.vector_cosine_ops) WITH (lists='100');


--
-- Name: person_group_last_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX person_group_last_seen ON public.person_group USING btree (last_seen DESC);


--
-- Name: appearance appearance_cam_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.appearance
    ADD CONSTRAINT appearance_cam_id_fkey FOREIGN KEY (cam_id) REFERENCES public.cameras(id);


--
-- Name: appearance_crop appearance_crop_appearance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.appearance_crop
    ADD CONSTRAINT appearance_crop_appearance_id_fkey FOREIGN KEY (appearance_id) REFERENCES public.appearance(id) ON DELETE CASCADE;


--
-- Name: appearance appearance_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.appearance
    ADD CONSTRAINT appearance_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.person_group(id) ON DELETE CASCADE;


--
-- Name: events events_cam_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_cam_id_fkey FOREIGN KEY (cam_id) REFERENCES public.cameras(id);


--
-- Name: person_group person_group_cam_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.person_group
    ADD CONSTRAINT person_group_cam_id_fkey FOREIGN KEY (cam_id) REFERENCES public.cameras(id);


--
-- PostgreSQL database dump complete
--

\unrestrict UAIbi5vKdkhsbpbjBoZkJIapfe5j0nor6r0HVFh3NixCvzk1MsnJ1D3kwKLQvrU

