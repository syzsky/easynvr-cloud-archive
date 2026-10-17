#!/bin/bash
cd /opt/easynvr-cloud-archive && EASYNVR_CFG=/opt/easynvr-cloud-archive/config.conf FORCE_RUN=1 bash easynvr_cloud_archive.sh >> /var/log/easynvr_cloud_archive_cron.log 2>&1
