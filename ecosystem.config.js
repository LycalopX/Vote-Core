module.exports = {
  apps: [
    {
      name: "vote-core",
      script: ".venv/bin/uvicorn",
      args: "app.main:app --host 0.0.0.0 --port 2029 --proxy-headers --forwarded-allow-ips=*",
      cwd: "/home/lycalopx/repos/Vote-Core",
      interpreter: "none",
      autorestart: true,
      watch: false,
      max_memory_restart: "512M",
      env: {
        PYTHONUNBUFFERED: "1",
      },
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      error_file: "/home/lycalopx/repos/Vote-Core/logs/pm2-error.log",
      out_file: "/home/lycalopx/repos/Vote-Core/logs/pm2-out.log",
      merge_logs: true,
    },
  ],
};
