-- Release 2.4. Also the upgrade path for databases that marked the baseline as applied.

ALTER TABLE "Plant"
  ALTER COLUMN "price" TYPE DECIMAL(10,2) USING ROUND("price"::numeric, 2),
  ALTER COLUMN "weight_kg" TYPE DECIMAL(8,3) USING ROUND("weight_kg"::numeric, 3);

ALTER TABLE "Order" ALTER COLUMN "total" TYPE DECIMAL(10,2) USING ROUND("total"::numeric, 2);

-- Low-stock alerts
CREATE TABLE IF NOT EXISTS "StockAlert" (
    "id" TEXT NOT NULL,
    "plantId" TEXT NOT NULL,
    "threshold" INTEGER NOT NULL,
    CONSTRAINT "StockAlert_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX IF NOT EXISTS "StockAlert_plantId_key" ON "StockAlert"("plantId");

ALTER TABLE "StockAlert" ADD CONSTRAINT "StockAlert_plantId_fkey" FOREIGN KEY ("plantId") REFERENCES "Plant"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- Orders remember where they came from
ALTER TABLE "Order" ADD COLUMN "giftNote" TEXT,
ADD COLUMN "channel" TEXT NOT NULL;

-- Reporting index, created from a DO block so it can be skipped where it exists
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'Order_createdAt_idx') THEN
    EXECUTE 'CREATE INDEX "Order_createdAt_idx" ON "Order" ("createdAt")';
  END IF;
END $$;

-- Customers: the phone column becomes mobile, and the old import reference goes
ALTER TABLE "Customer" RENAME COLUMN "phone" TO "mobile";

ALTER TABLE "Customer" DROP COLUMN "legacy_ref";
