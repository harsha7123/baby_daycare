FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# CUDA wheels bundle their own CUDA runtime; the host only needs the NVIDIA driver + container toolkit.
RUN pip install --no-cache-dir torch==2.11.0 torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128
COPY pyproject.toml ./
COPY vdb ./vdb
RUN pip install --no-cache-dir ".[ml,gpu]"

# Sample clip used as a stand-in camera until real CCTV is connected (configs/cloud.example.yaml).
RUN mkdir -p /app/samples && cd /app/samples \
    && python -c "from supervision.assets import download_assets, VideoAssets; download_assets(VideoAssets.PEOPLE_WALKING)"

RUN useradd --create-home vdb && mkdir -p /app/data && chown vdb /app/data
USER vdb
# Model weights and TensorRT engines live on the persistent volume (downloaded / built once).
ENV HF_HOME=/app/data/hf RF_HOME=/app/data/rf
ENTRYPOINT ["vdb", "--config", "/app/configs/cloud.yaml"]
