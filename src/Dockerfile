# Container image for the geospatial Lambda.
# The manylinux wheels for rasterio/numpy bundle their own GDAL, PROJ and GEOS,
# so no OS-level geo packages are required inside the AWS Lambda base image.
FROM public.ecr.aws/lambda/python:3.11

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    GDAL_CACHEMAX=256 \
    VSI_CACHE=TRUE

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt \
 && find ${LAMBDA_TASK_ROOT} -name "__pycache__" -type d -prune -exec rm -rf {} + \
 && find ${LAMBDA_TASK_ROOT} -name "*.pyc" -delete

COPY handler.py indices.py ${LAMBDA_TASK_ROOT}/

# Lambda container contract: handler = <module>.<function>
CMD ["handler.lambda_handler"]
