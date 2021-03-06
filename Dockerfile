FROM python:3.8

ENV PATH /opt/conda/bin:$PATH

# taken from https://github.com/ContinuumIO/docker-images/blob/master/miniconda3/debian/Dockerfile
# Leave these args here to better use the Docker build cache
ARG CONDA_VERSION=py38_4.9.2
ARG CONDA_MD5=122c8c9beb51e124ab32a0fa6426c656

RUN apt-get update
RUN apt-get upgrade -y
RUN apt-get install redis-server -y

RUN wget --quiet https://repo.anaconda.com/miniconda/Miniconda3-${CONDA_VERSION}-Linux-x86_64.sh -O miniconda.sh && \
    echo "${CONDA_MD5}  miniconda.sh" > miniconda.md5 && \
    if ! md5sum --status -c miniconda.md5; then exit 1; fi && \
    mkdir -p /opt && \
    sh miniconda.sh -b -p /opt/conda && \
    rm miniconda.sh miniconda.md5 && \
    ln -s /opt/conda/etc/profile.d/conda.sh /etc/profile.d/conda.sh && \
    echo ". /opt/conda/etc/profile.d/conda.sh" >> ~/.bashrc && \
    echo "conda activate base" >> ~/.bashrc && \
    find /opt/conda/ -follow -type f -name '*.a' -delete && \
    find /opt/conda/ -follow -type f -name '*.js.map' -delete && \
    /opt/conda/bin/conda clean -afy

COPY ./conda_environment.yaml conda_environment.yaml
RUN conda env create -f conda_environment.yaml

#RUN git clone https://github.com/zack-kimble/photo_gallery_wip.git
WORKDIR photo_gallery
COPY . .
RUN ls -a

RUN chmod 777 entrypoint.sh
#ENTRYPOINT ["./conda_activate.sh"] #not login, conda init fails
#ENTRYPOINT ["/bin/bash","--login"] #works, but in base (same as no --login option)
#ENTRYPOINT ["/bin/bash","--login", "-c", "conda activate photo_gallery",] #works, but doesn't accept addtional command.
#ENTRYPOINT ["/bin/bash","--login", "-c", "conda activate photo_gallery", "exec","$@"] #seems the same here
#ENTRYPOINT ["./conda_activate.sh"]
#ENTRYPOINT ["/bin/bash"]
#ENTRYPOINT ["/bin/bash","--login", "-c", "conda activate photo_gallery", "exec","$@"] #works, not interactive
#with new activate script using set -e
ENTRYPOINT ["./entrypoint.sh"]
CMD ["bash", "start_server.sh"]