# Runbook notes

## The website does not open
No manual security-group edits are needed any more: `web.yaml` opens port 80 for everyone and port 22 only for the
EC2 Instance Connect range (parameter `InstanceConnectCidr`). The stack also waits for the app to answer `/healthz`
before it reports success, and the pipeline then fetches both login pages. If a deployment fails, read the reason in
the CloudFormation events of the `-web` stack, then on the instance (Session Manager needs no inbound rule):

    sudo tail -n 60 /var/log/medislot-bootstrap.log     # what the boot script did
    sudo systemctl status medislot-web --no-pager -l    # is the service running
    sudo journalctl -u medislot-web -n 80 --no-pager    # application errors
    curl -i http://localhost/healthz

## Connecting to the instance
* Preferred: EC2 console -> Connect -> **Session Manager** (works with no inbound rules).
* EC2 Instance Connect works because of the port 22 rule above. If your region is not `ap-south-1`, set
  `InstanceConnectCidr` to the `EC2_INSTANCE_CONNECT` range for that region from
  https://ip-ranges.amazonaws.com/ip-ranges.json (or set it to an empty value to close port 22).

## Sessions
The web app keeps sessions in the memory of its single gunicorn worker. Do not raise `--workers` above 1 in
`web.yaml`; restarting the service signs everybody out.

## Deleting leftovers from an older checkout
If these files reappear after a merge, remove them again (they are unused and some break the lint step):

    git rm application/auth/login.py application/auth/logout.py application/auth/refresh.py application/auth/register.py \
           application/common/auth_utils.py infrastructure/cloudformation/application.yaml \
           infrastructure/cloudformation/iam.yaml infrastructure/cloudformation/scheduler.yaml \
           tests/unit/test_access_token_chain.py
