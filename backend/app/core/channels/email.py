import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def send_email(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    to_addr: str,
    subject: str,
    content: str,
    use_tls: bool = True,
) -> None:
    msg = MIMEMultipart()
    msg["From"] = username
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(content, "plain", "utf-8"))

    if use_tls:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(username, password)
            server.sendmail(username, to_addr, msg.as_string())
    else:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, to_addr, msg.as_string())
