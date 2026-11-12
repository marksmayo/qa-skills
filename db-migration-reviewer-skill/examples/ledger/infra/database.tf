# Production database (sample; nothing here is real).
resource "aws_db_instance" "ledger" {
  identifier          = "ledger-prod"
  engine              = "postgres"
  engine_version      = "16.4"
  instance_class      = "db.r6g.large"
  allocated_storage   = 200
  deletion_protection = true
}
