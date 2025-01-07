# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    wget \
    unzip \
    libboost-all-dev \
    libtinyxml2-dev \
    libopenblas-dev \
    libeigen3-dev \
    libtbb-dev \
    libpng-dev \
    libopenexr-dev \
    libglew-dev \
    libglfw3-dev \
    libglm-dev \
    && rm -rf /var/lib/apt/lists/*

# Install ifcconvert binary from .zip release
RUN wget https://github.com/IfcOpenShell/IfcOpenShell/releases/download/ifcconvert-0.8.0/ifcconvert-0.8.0-linux64.zip && \
    unzip ifcconvert-0.8.0-linux64.zip && \
    mv IfcConvert /usr/local/bin/ifcconvert && \
    chmod +x /usr/local/bin/ifcconvert && \
    rm ifcconvert-0.8.0-linux64.zip

# Verify ifcconvert installation
RUN ifcconvert --version

# Set work directory
WORKDIR /app

# Copy requirements.txt and install Python dependencies
COPY requirements.txt .

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Copy the application code
COPY main.py .

# Expose port
EXPOSE 8501

# Set environment variables for Streamlit
ENV STREAMLIT_SERVER_ENABLECORS=false
ENV STREAMLIT_SERVER_PORT=8501

# Run the Streamlit app
CMD ["streamlit", "run", "main.py"]
