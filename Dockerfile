FROM python:3.13-slim

# Hugging Face Spaces runs the container as uid 1000.
RUN useradd -m -u 1000 user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=100 PIP_RETRIES=10 \
    HF_HOME=/home/user/.cache/huggingface \
    ATLAS_DATA_DIR=/home/user/app/data \
    APP_ENV=production PORT=7860

# Build deps for lxml/chromadb wheels are normally prebuilt; keep the image small.
# USER comes before WORKDIR so the app directory is owned by uid 1000 (writable data/).
USER user
WORKDIR /home/user/app

# CPU-only PyTorch first (much smaller than the default CUDA build), then the rest.
COPY --chown=user requirements.txt .
RUN pip install --user torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --user -r requirements.txt

# Use the ONNX build of the embedding model (no PyTorch at run time) so the app fits in 512 MB.
# Bake it into the image so startup never needs the network.
ENV ATLAS_EMBEDDING_BACKEND=onnx ATLAS_DENSE_RANKING=0 ATLAS_MAX_TOP_K=6
RUN python -c "from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2; ONNXMiniLM_L6_V2()(['warm up'])"

COPY --chown=user . .
RUN mkdir -p data/sqlite data/chroma data/logs

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/ready' % os.environ.get('PORT','7860'),timeout=4)" || exit 1
CMD ["sh", "deploy/start_space.sh"]
