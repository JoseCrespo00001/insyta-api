-- GRANTs del rol runtime insyta_app sobre el schema public.
-- Se aplican DESPUÉS de `alembic upgrade head` (las tablas ya existen).
-- Reproducible: el mismo archivo sirve para el stack local y para RDS.
-- insyta_app es NOBYPASSRLS → queda sujeto a las policies RLS (org_isolation, etc.).

GRANT USAGE ON SCHEMA public TO insyta_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO insyta_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO insyta_app;

-- Futuras tablas/sequences (por si se corren migraciones nuevas con el owner).
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO insyta_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO insyta_app;
