"""Offline structural checks for the Step 41 AWS deployment files."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    compose = (ROOT / "compose.production.yaml").read_text()
    caddy = (ROOT / "deploy/Caddyfile").read_text()
    main_tf = (ROOT / "infrastructure/aws/main.tf").read_text()
    gitignore = (ROOT / "infrastructure/aws/.gitignore").read_text()

    assert "OPENAI_API_KEY" not in compose
    assert "127.0.0.1:8000" in compose
    assert 'network_mode: "service:gateway"' in compose
    assert '"80:80"' in compose
    assert "/admin/v1" not in caddy
    assert "Managed-CachingDisabled" in main_tf
    assert "com.amazonaws.global.cloudfront.origin-facing" in main_tf
    assert 'http_tokens   = "required"' in main_tf
    assert "AmazonSSMManagedInstanceCore" in main_tf
    assert "0.0.0.0/0" not in main_tf.split('resource "aws_security_group" "app"', 1)[1].split('resource "aws_iam_role"', 1)[0].split("ingress", 1)[1].split("egress", 1)[0]
    assert "*.tfstate" in gitignore

    print("Step 41 static checks passed: HTTPS edge, restricted origin, SSM, IMDSv2, and ignored Terraform state.")


if __name__ == "__main__":
    main()
