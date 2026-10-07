# An optional way out to the internet, for tracing. Added for Week 6, Stop 8.
#
# The original stack has no route out of the VPC, and needs none: every AWS service it
# calls has a VPC endpoint. Langfuse Cloud is not an AWS service, so without a route out
# the functions have nowhere to send traces.
#
#   enable_nat = true    one NAT gateway, in the first Availability Zone only
#   enable_nat = false   the original network, exactly as before
#
# What it costs, us-east-1:
#   NAT gateway        $0.045 an hour     about $1.08 a day
#   public IPv4        $0.005 an hour     about $0.12 a day
#   data processed     $0.045 per GB      traces are a few KB each, so pennies
#
# Every VPC endpoint stays. Bedrock, Secrets Manager and OpenSearch names still resolve to
# the endpoints through private DNS, and S3 has its own, more specific, route. So AWS
# traffic never passes through the NAT and is never billed for it. Only Langfuse does.
#
# One NAT, not one per zone: if its zone fails, tracing stops and answering does not.
# Both private subnets share one route table, so the second zone reaches the NAT across
# zones, which adds $0.01 per GB. A production stack would put one in each zone.
#
# What it changes about the network: the functions can now open HTTPS connections to any
# address on the internet, not only to AWS. The security group already allowed port 443
# to anywhere; until now there was simply no route. Aurora is unaffected. It has no public
# address and accepts connections only from the Lambda security group.

locals {
  nat_count = var.enable_nat ? 1 : 0
}

resource "aws_internet_gateway" "main" {
  count  = local.nat_count
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-igw" }
}

# The NAT gateway needs a subnet that does have a route to the internet gateway.
# Nothing else is ever placed in it.
resource "aws_subnet" "public" {
  count                   = local.nat_count
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, 0) # 10.20.0.0/24
  availability_zone       = local.azs[0]
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-public-${local.azs[0]}" }
}

resource "aws_route_table" "public" {
  count  = local.nat_count
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main[0].id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = local.nat_count
  subnet_id      = aws_subnet.public[0].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"

  tags       = { Name = "${local.name}-nat" }
  depends_on = [aws_internet_gateway.main]
}

resource "aws_nat_gateway" "main" {
  count         = local.nat_count
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id

  tags       = { Name = "${local.name}-nat" }
  depends_on = [aws_internet_gateway.main]
}

# The private subnets' default route. More specific routes win, so the S3 gateway
# endpoint's route still carries all S3 traffic.
resource "aws_route" "private_to_nat" {
  count                  = local.nat_count
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[0].id
}
