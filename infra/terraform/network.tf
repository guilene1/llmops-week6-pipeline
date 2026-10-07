# The private network. Nothing in it has a public IP address.
#
#   Aurora and the Lambda functions live in two private subnets, in two Availability Zones.
#   Lambda reaches AWS services through VPC endpoints (endpoints.tf), so traffic
#   to Bedrock, Secrets Manager, S3 and OpenSearch never leaves the AWS network.
#
#   With enable_nat = true (the default in this repository), nat.tf adds an internet
#   gateway and one NAT gateway, for tracing to Langfuse Cloud. With false there is no
#   internet gateway and no route out, as in the original stack.

locals {
  name = var.project_name
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true # needed for the private DNS names of VPC endpoints
  enable_dns_hostnames = true

  tags = { Name = "${local.name}-vpc" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index + 1) # 10.20.1.0/24, 10.20.2.0/24
  availability_zone = local.azs[count.index]

  tags = { Name = "${local.name}-private-${local.azs[count.index]}" }
}

# The S3 endpoint adds its own route here, and nat.tf adds a default route when enable_nat is on.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
