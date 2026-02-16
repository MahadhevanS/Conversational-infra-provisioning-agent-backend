resource "null_resource" "compute_mock" {

  triggers = {
    instance_type = var.instance_type
    environment   = var.environment
  }

}
