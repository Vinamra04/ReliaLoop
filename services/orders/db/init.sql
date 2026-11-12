-- Orders schema. Applied once by the Postgres image on first start of an empty volume.
CREATE TABLE IF NOT EXISTS orders (
    id          uuid PRIMARY KEY,
    customer    text NOT NULL,
    items       jsonb NOT NULL,
    total_cents int NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS orders_created_at_idx ON orders (created_at DESC);
