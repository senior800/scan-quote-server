# S-CAN quoting geometry service (Phase 2, milestone 2a)
# pythonocc-core (OpenCascade) is only packaged on conda-forge, so we build on
# micromamba rather than a plain python image.
FROM mambaorg/micromamba:1.5.8

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install -y -n base -f /tmp/environment.yml && micromamba clean --all --yes

COPY --chown=$MAMBA_USER:$MAMBA_USER . /app
WORKDIR /app

EXPOSE 8000
# `micromamba run` activates the env so OCC/trimesh are importable.
CMD ["micromamba", "run", "-n", "base", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
