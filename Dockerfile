# -------------------------------
# Base Image
# -------------------------------
FROM python:3.11-slim-bookworm

# -------------------------------
# Environment Variables
# -------------------------------
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# -------------------------------
# Install System Dependencies
# -------------------------------
RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    git \
    bash \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# -------------------------------
# Install Terraform
# -------------------------------
ARG TERRAFORM_VERSION=1.6.6

RUN curl -fsSL https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip -o terraform.zip \
    && unzip terraform.zip \
    && mv terraform /usr/local/bin/ \
    && rm terraform.zip

# -------------------------------
# Install Infracost
# -------------------------------
RUN curl -fsSL https://raw.githubusercontent.com/infracost/infracost/master/scripts/install.sh | sh

# -------------------------------
# Set Work Directory
# -------------------------------
WORKDIR /app

# -------------------------------
# Copy Requirements
# -------------------------------
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# -------------------------------
# Copy Project Files
# -------------------------------
COPY . .

# -------------------------------
# Create Persistent Jobs Directory
# -------------------------------
RUN mkdir -p /app/persistent_jobs

# -------------------------------
# Expose Port
# -------------------------------
EXPOSE 8000

# -------------------------------
# Start Application
# -------------------------------
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000","--reload"]