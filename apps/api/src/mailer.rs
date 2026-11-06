use crate::config::AppConfig;
use anyhow::{Context, Result};
use chrono::{DateTime, SecondsFormat, Utc};
use lettre::{
    message::header::ContentType, transport::smtp::authentication::Credentials, AsyncSmtpTransport,
    AsyncTransport, Message, Tokio1Executor,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SmtpSecurity {
    ImplicitTls,
    StartTls,
    Plaintext,
}

fn smtp_security_for_port(port: u16) -> SmtpSecurity {
    match port {
        465 => SmtpSecurity::ImplicitTls,
        587 | 2525 => SmtpSecurity::StartTls,
        _ => SmtpSecurity::Plaintext,
    }
}

#[derive(Debug)]
pub struct Mailer {
    transport: AsyncSmtpTransport<Tokio1Executor>,
    host: String,
    port: u16,
    user: Option<String>,
    from: String,
    #[allow(clippy::type_complexity)]
    test_sink: Option<std::sync::Arc<std::sync::Mutex<Vec<(String, String)>>>>,
}

fn escape_html(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

fn magic_link_email(link: &str, requested_at: DateTime<Utc>) -> (String, String) {
    let request_label = requested_at.to_rfc3339_opts(SecondsFormat::Secs, true);
    let subject = format!("Log in to commonThing · {request_label}");
    let safe_link = escape_html(link);
    let body = format!(
        r#"<!DOCTYPE html>
<html>
<body style="font-family: sans-serif; padding: 20px;">
    <h2>Log in to commonThing</h2>
    <p>Click the link below to sign in:</p>
    <p style="margin: 20px 0;">
        <a href="{safe_link}" style="background-color: #0070f3; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Sign In</a>
    </p>
    <p style="color: #444; font-size: 0.9em;"><strong>Requested:</strong> {request_label}</p>
    <p style="color: #666; font-size: 0.9em;">Only the newest sign-in email for your address is valid. This link expires soon.</p>
    <p style="color: #666; font-size: 0.9em;">Button not working? Copy this address into your browser:</p>
    <p style="overflow-wrap: anywhere; font-size: 0.85em;"><a href="{safe_link}">{safe_link}</a></p>
    <p style="color: #666; font-size: 0.9em;">If you did not request this email, you can safely ignore it.</p>
</body>
</html>"#
    );
    (subject, body)
}

impl Mailer {
    pub fn new(config: &AppConfig) -> Result<Self> {
        let host = config.smtp_host.as_deref().context("SMTP_HOST missing")?;
        let port = config.smtp_port.unwrap_or(587);
        let from = config.smtp_from.clone().context("SMTP_FROM missing")?;

        let auth_policy = std::env::var("SMTP_AUTH")
            .unwrap_or_else(|_| "auto".to_string())
            .to_lowercase();

        let (user, pass) = match auth_policy.as_str() {
            "off" => (None, None),
            "on" => {
                let user = config
                    .smtp_user
                    .as_deref()
                    .context("SMTP_USER missing (SMTP_AUTH=on)")?;
                let pass = config
                    .smtp_pass
                    .as_deref()
                    .context("SMTP_PASS missing (SMTP_AUTH=on)")?;
                (Some(user), Some(pass))
            }
            _ => match (config.smtp_user.as_deref(), config.smtp_pass.as_deref()) {
                (Some(user), Some(pass)) => (Some(user), Some(pass)),
                _ => (None, None),
            },
        };

        let creds = match (user, pass) {
            (Some(user), Some(pass)) => Some(Credentials::new(user.to_string(), pass.to_string())),
            _ => None,
        };

        if from.parse::<lettre::message::Mailbox>().is_err() {
            anyhow::bail!("invalid from address: {}", from);
        }

        let smtp_security = smtp_security_for_port(port);
        if smtp_security == SmtpSecurity::Plaintext && creds.is_some() {
            anyhow::bail!("refusing SMTP authentication without TLS on port {port}");
        }

        let transport = match smtp_security {
            SmtpSecurity::ImplicitTls => {
                let mut builder = AsyncSmtpTransport::<Tokio1Executor>::relay(host)
                    .context("failed to init SMTP relay (implicit TLS, port 465)")?
                    .port(port);
                if let Some(creds) = creds.as_ref() {
                    builder = builder.credentials(creds.clone());
                }
                builder.build()
            }
            SmtpSecurity::StartTls => {
                let mut builder = AsyncSmtpTransport::<Tokio1Executor>::starttls_relay(host)
                    .with_context(|| format!("failed to init SMTP relay (STARTTLS, port {port})"))?
                    .port(port);
                if let Some(creds) = creds.as_ref() {
                    builder = builder.credentials(creds.clone());
                }
                builder.build()
            }
            SmtpSecurity::Plaintext => {
                let mut builder =
                    AsyncSmtpTransport::<Tokio1Executor>::builder_dangerous(host).port(port);
                if let Some(creds) = creds.as_ref() {
                    builder = builder.credentials(creds.clone());
                }
                builder.build()
            }
        };

        Ok(Self {
            transport,
            from,
            host: host.to_string(),
            port,
            user: user.map(str::to_string),
            test_sink: None,
        })
    }

    pub fn with_test_sink(
        mut self,
        sink: std::sync::Arc<std::sync::Mutex<Vec<(String, String)>>>,
    ) -> Self {
        self.test_sink = Some(sink);
        self
    }

    pub async fn send_step_up_magic_link(&self, to: &str, link: &str) -> Result<()> {
        if let Some(sink) = &self.test_sink {
            sink.lock()
                .expect("mailer test_sink mutex poisoned")
                .push((to.to_string(), link.to_string()));
            return Ok(());
        }

        let safe_link = escape_html(link);
        let email = Message::builder()
            .from(self.from.parse().context("invalid from address")?)
            .to(to.parse().context("invalid to address")?)
            .subject("Confirm sensitive action in commonThing")
            .header(ContentType::TEXT_HTML)
            .body(format!(
                r#"<!DOCTYPE html>
<html>
<body style="font-family: sans-serif; padding: 20px;">
    <h2>Confirm your action in commonThing</h2>
    <p>Click the link below to confirm your sensitive action. This link is strictly bound to your current session.</p>
    <p style="margin: 20px 0;"><a href="{safe_link}" style="background-color: #d32f2f; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Confirm Action</a></p>
    <p style="color: #666; font-size: 0.9em;">This link expires in a few minutes.</p>
    <p style="color: #666; font-size: 0.9em;">If you did not request this email, please secure your account.</p>
</body>
</html>"#
            ))
            .context("failed to build email")?;

        self.transport
            .send(email)
            .await
            .context("failed to send step-up magic link email")?;
        Ok(())
    }

    pub async fn send_magic_link(&self, to: &str, link: &str) -> Result<()> {
        if let Some(sink) = &self.test_sink {
            sink.lock()
                .expect("mailer test_sink mutex poisoned")
                .push((to.to_string(), link.to_string()));
            return Ok(());
        }

        let (subject, body) = magic_link_email(link, Utc::now());
        let email = Message::builder()
            .from(self.from.parse().context("invalid from address")?)
            .to(to.parse().context("invalid to address")?)
            .subject(subject)
            .header(ContentType::TEXT_HTML)
            .body(body)
            .context("failed to build email")?;

        self.transport.send(email).await.with_context(|| {
            format!(
                "smtp send failed (host={:?} port={:?} user={:?} from={:?})",
                self.host, self.port, self.user, self.from
            )
        })?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::AppConfig;
    use chrono::TimeZone;
    use serial_test::serial;

    #[test]
    fn smtp_submission_ports_use_expected_tls_modes() {
        assert_eq!(smtp_security_for_port(465), SmtpSecurity::ImplicitTls);
        assert_eq!(smtp_security_for_port(587), SmtpSecurity::StartTls);
        assert_eq!(smtp_security_for_port(2525), SmtpSecurity::StartTls);
        assert_eq!(smtp_security_for_port(1025), SmtpSecurity::Plaintext);
    }

    #[test]
    fn repeated_login_email_has_request_identity_and_fallback_link() {
        let requested_at = Utc.with_ymd_and_hms(2026, 7, 10, 6, 42, 43).unwrap();
        let link = "https://commonthing.net/api/auth/magic-link/consume?token=a&next=<home>";
        let (subject, body) = magic_link_email(link, requested_at);

        assert_eq!(subject, "Log in to commonThing · 2026-07-10T06:42:43Z");
        assert!(body.contains("Requested:</strong> 2026-07-10T06:42:43Z"));
        assert!(body.contains("Only the newest sign-in email for your address is valid."));
        assert!(body.contains("Button not working?"));
        assert!(body.contains("&amp;next=&lt;home&gt;"));
        assert!(!body.contains("&next=<home>"));
    }

    #[test]
    #[serial]
    fn mailer_rejects_credentials_on_plaintext_port() {
        let _smtp_auth = crate::test_helpers::EnvGuard::set("SMTP_AUTH", "on");
        let config = AppConfig {
            max_guest_owned_nodes: 1_000,
            domain_read_source: crate::config::DomainReadSource::Jsonl,
            domain_account_write_source: crate::config::DomainAccountWriteSource::Jsonl,
            domain_node_write_source: crate::config::DomainNodeWriteSource::Jsonl,
            domain_edge_write_source: crate::config::DomainEdgeWriteSource::Jsonl,
            passkey_credential_source: crate::config::PasskeyCredentialSource::InMemory,
            auth_public_login: false,
            auth_cookie_secure: crate::config::auth_cookie_secure_env_override().unwrap_or(true),
            app_base_url: None,
            auth_trusted_proxies: None,
            auth_allow_emails: None,
            auth_allow_email_domains: None,
            auth_auto_provision: false,
            auth_auto_provision_role: crate::config::AutoProvisionRole::Gast,
            auth_rl_ip_per_min: None,
            auth_rl_ip_per_hour: None,
            auth_rl_email_per_min: None,
            auth_rl_email_per_hour: None,
            search_rl_ip_per_min: None,
            search_rl_ip_per_hour: None,
            node_mutation_rate_limits: Default::default(),
            smtp_host: Some("127.0.0.1".to_string()),
            smtp_port: Some(1025),
            smtp_user: Some("user".to_string()),
            smtp_pass: Some("pass".to_string()),
            smtp_from: Some("noreply@example.com".to_string()),
            auth_log_magic_token: false,
            webauthn_rp_id: None,
            webauthn_rp_origin: None,
            webauthn_rp_name: None,
        };

        let result = Mailer::new(&config);
        assert!(result.is_err());
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("refusing SMTP authentication without TLS on port 1025"));
    }

    #[test]
    #[serial]
    fn mailer_fails_with_invalid_from_address() {
        let config = AppConfig {
            max_guest_owned_nodes: 1_000,
            domain_read_source: crate::config::DomainReadSource::Jsonl,
            domain_account_write_source: crate::config::DomainAccountWriteSource::Jsonl,
            domain_node_write_source: crate::config::DomainNodeWriteSource::Jsonl,
            domain_edge_write_source: crate::config::DomainEdgeWriteSource::Jsonl,
            passkey_credential_source: crate::config::PasskeyCredentialSource::InMemory,
            auth_public_login: false,
            auth_cookie_secure: crate::config::auth_cookie_secure_env_override().unwrap_or(true),
            app_base_url: None,
            auth_trusted_proxies: None,
            auth_allow_emails: None,
            auth_allow_email_domains: None,
            auth_auto_provision: false,
            auth_auto_provision_role: crate::config::AutoProvisionRole::Gast,
            auth_rl_ip_per_min: None,
            auth_rl_ip_per_hour: None,
            auth_rl_email_per_min: None,
            auth_rl_email_per_hour: None,
            search_rl_ip_per_min: None,
            search_rl_ip_per_hour: None,
            node_mutation_rate_limits: Default::default(),
            smtp_host: Some("127.0.0.1".to_string()),
            smtp_port: Some(1025),
            smtp_user: Some("user".to_string()),
            smtp_pass: Some("pass".to_string()),
            smtp_from: Some("not-an-email".to_string()),
            auth_log_magic_token: false,
            webauthn_rp_id: None,
            webauthn_rp_origin: None,
            webauthn_rp_name: None,
        };

        let result = Mailer::new(&config);
        assert!(result.is_err());
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("invalid from address"));
    }
}
