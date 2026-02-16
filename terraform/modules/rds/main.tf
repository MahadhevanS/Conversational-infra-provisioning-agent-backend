resource "aws_db_subnet_group" "this" {
  name       = "${var.environment}-db-subnet-group"
  subnet_ids = var.subnet_ids
}

resource "aws_db_instance" "this" {
  allocated_storage    = 20
  engine               = "mysql"
  instance_class       = var.instance_type
  username             = "adminuser"
  password             = "adminpassword123"
  skip_final_snapshot  = true

  db_subnet_group_name = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.security_group_id]

  tags = {
    Name        = "${var.environment}-rds"
    Environment = var.environment
  }
}
