terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0, < 6.0"
    }
  }
}

provider "google" {
  project = var.nonprod_project_id
  region  = var.region
}

# Separate provider alias for the prod project (IAM binding only).
provider "google" {
  alias   = "prod"
  project = var.prod_project_id
  region  = var.region
}
