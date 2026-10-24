curl --url 'smtp://smtp.email.eu-zurich-1.oci.oraclecloud.com:587' \
  --ssl-reqd \
  --mail-from 'merlin.banani@gmail.com' \
  --mail-rcpt 'dwian.pramono@gmail.com' \
  --user 'IHRE_OCI_SMTP_BENUTZERNAME:IHRE_OCI_SMTP_PASSWORT' \
  -T - <<EOF
From: merlin.banani@gmail.com
To: dwian.pramono@gmail.com
Subject: OCI SMTP CLI Test

Hallo, dies ist ein direkter Test über OCI SMTP.
EOFs 