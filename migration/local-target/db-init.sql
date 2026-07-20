-- Réplica LOCAL del setup que en Supabase se hizo a mano (fuera del repo).
-- Corre en el initdb del Postgres del stack destino local.

-- Esquema de GoTrue (self-host). GoTrue crea sus tablas adentro vía sus migraciones.
CREATE SCHEMA IF NOT EXISTS auth;

-- Rol de la app: LOGIN, NOBYPASSRLS (para que las policies RLS se ejerzan de verdad).
-- El rol owner (postgres) corre Alembic; insyta_app es el runtime.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'insyta_app') THEN
    CREATE ROLE insyta_app LOGIN PASSWORD 'insyta_app_local' NOBYPASSRLS;
  END IF;
END
$$;

GRANT CONNECT ON DATABASE insyta TO insyta_app;
GRANT USAGE ON SCHEMA public TO insyta_app;
-- Los GRANTs finos sobre tablas + default privileges se aplican DESPUÉS de alembic
-- (ver migration/local-target/setup.sh), porque las tablas aún no existen acá.
