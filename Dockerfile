# Lightweight Python Installation
FROM python:3.9-slim

# Makes app the working directory
WORKDIR /app

# Download requests
RUN pip install requests

CMD ["/bin/bash"]