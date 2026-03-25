#!/bin/bash
set -e

export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new"
if [ -z "$(ls /vault)" ] ; then
    git clone "$GIT_UPSTREAM" /vault
fi

git config --global user.email "$GIT_EMAIL"
git config --global user.name "$GIT_NAME"
git -C /vault reset --hard HEAD
git -C /vault clean -f

exec gunicorn -w 1 -b 0.0.0.0:5000 nether_portal:app
