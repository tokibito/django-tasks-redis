# Prometheus Metrics Integration

`django-tasks-redis` provides optional Prometheus metrics integration for monitoring task queue performance.

## Installation

Install with the `prometheus` extra:

```bash
pip install django-tasks-redis[prometheus]
```

This installs the `prometheus-client` package as an additional dependency.

## Configuration

Enable metrics in your Django settings:

```python
TASKS = {
    "default": {
        "BACKEND": "django_tasks_redis.RedisTaskBackend",
        "QUEUES": [],
        "OPTIONS": {
            "REDIS_URL": "redis://localhost:6379/0",
            "ENABLE_METRICS": True,  # Enable Prometheus metrics
        },
    },
}
```

## Available Metrics

### Counters

**`django_tasks_enqueued_total`**
- Description: Total number of tasks enqueued
- Labels: `backend`, `queue`, `priority`
- Example: `django_tasks_enqueued_total{backend="default",queue="default",priority="0"} 1234`

**`django_tasks_completed_total`**
- Description: Total number of tasks completed successfully
- Labels: `backend`, `queue`
- Example: `django_tasks_completed_total{backend="default",queue="default"} 1200`

**`django_tasks_failed_total`**
- Description: Total number of tasks that failed
- Labels: `backend`, `queue`
- Example: `django_tasks_failed_total{backend="default",queue="default"} 34`

### Gauges

**`django_tasks_queue_length`**
- Description: Current number of tasks in queue by status
- Labels: `backend`, `queue`, `status`
- Status values: `READY`, `RUNNING`, `SUCCESSFUL`, `FAILED`
- Example: `django_tasks_queue_length{backend="default",queue="all",status="READY"} 42`

**`django_tasks_running`**
- Description: Number of currently running tasks
- Labels: `backend`, `queue`
- Example: `django_tasks_running{backend="default",queue="default"} 5`

### Histograms

**`django_tasks_duration_seconds`**
- Description: Task execution duration in seconds
- Labels: `backend`, `queue`, `status`
- Buckets: `0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, +Inf`
- Example:
  ```
  django_tasks_duration_seconds_bucket{backend="default",queue="default",status="SUCCESSFUL",le="1.0"} 450
  django_tasks_duration_seconds_sum{backend="default",queue="default",status="SUCCESSFUL"} 12345.67
  django_tasks_duration_seconds_count{backend="default",queue="default",status="SUCCESSFUL"} 1200
  ```

## Exposing Metrics Endpoint

### Option 1: Using Django Prometheus (Recommended)

Install `django-prometheus`:

```bash
pip install django-prometheus
```

Configure in settings:

```python
INSTALLED_APPS = [
    # ...
    "django_prometheus",
    "django_tasks_redis",
]

MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    # ... other middleware ...
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]
```

Add to URLs:

```python
urlpatterns = [
    # ...
    path("", include("django_prometheus.urls")),
]
```

Metrics will be available at: `http://localhost:8000/metrics`

### Option 2: Custom Endpoint

Create a custom view:

```python
# myapp/views.py
from django.http import HttpResponse
from prometheus_client import REGISTRY, generate_latest

def metrics_view(request):
    """Expose Prometheus metrics."""
    metrics = generate_latest(REGISTRY)
    return HttpResponse(metrics, content_type="text/plain; charset=utf-8")
```

Add to URLs:

```python
from myapp.views import metrics_view

urlpatterns = [
    # ...
    path("metrics/", metrics_view),
]
```

### Option 3: Separate Metrics Server

Run a dedicated metrics server in your worker process:

```python
# management/commands/run_worker_with_metrics.py
from django.core.management.base import BaseCommand
from prometheus_client import start_http_server
import threading

class Command(BaseCommand):
    help = "Run worker with Prometheus metrics server"

    def handle(self, *args, **options):
        # Start Prometheus metrics server on port 8001
        start_http_server(8001)
        self.stdout.write("Prometheus metrics server started on :8001")

        # Run your worker
        from django.core.management import call_command
        call_command("run_redis_tasks", continuous=True)
```

## Prometheus Configuration

Add a scrape config to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'django-tasks'
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/metrics'
    scrape_interval: 15s
```

## Example Grafana Dashboard

Here's a sample dashboard configuration:

### Queue Length Panel

```promql
# Current queue length by status
django_tasks_queue_length{backend="default"}
```

### Task Throughput Panel

```promql
# Tasks enqueued per minute
rate(django_tasks_enqueued_total[1m])

# Tasks completed per minute
rate(django_tasks_completed_total[1m])

# Tasks failed per minute
rate(django_tasks_failed_total[1m])
```

### Task Duration Panel

```promql
# 95th percentile task duration
histogram_quantile(0.95,
  rate(django_tasks_duration_seconds_bucket[5m])
)

# Average task duration
rate(django_tasks_duration_seconds_sum[5m]) /
rate(django_tasks_duration_seconds_count[5m])
```

### Success Rate Panel

```promql
# Success rate (%)
(
  rate(django_tasks_completed_total[5m]) /
  (rate(django_tasks_completed_total[5m]) + rate(django_tasks_failed_total[5m]))
) * 100
```

### Queue Backlog Panel

```promql
# Tasks waiting to be processed
django_tasks_queue_length{status="READY"}
```

## Alerting Rules

Example Prometheus alerting rules:

```yaml
groups:
  - name: django_tasks
    rules:
      # Alert if queue backlog is growing
      - alert: TaskQueueBacklog
        expr: django_tasks_queue_length{status="READY"} > 1000
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High task queue backlog"
          description: "Queue {{ $labels.queue }} has {{ $value }} pending tasks"

      # Alert if task failure rate is high
      - alert: HighTaskFailureRate
        expr: |
          (
            rate(django_tasks_failed_total[5m]) /
            (rate(django_tasks_completed_total[5m]) + rate(django_tasks_failed_total[5m]))
          ) > 0.1
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "High task failure rate"
          description: "Failure rate for queue {{ $labels.queue }} is {{ $value | humanizePercentage }}"

      # Alert if tasks are taking too long
      - alert: SlowTaskExecution
        expr: |
          histogram_quantile(0.95,
            rate(django_tasks_duration_seconds_bucket[5m])
          ) > 300
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Tasks taking too long"
          description: "95th percentile duration for queue {{ $labels.queue }} is {{ $value }}s"

      # Alert if no tasks are being processed
      - alert: NoTaskProcessing
        expr: rate(django_tasks_completed_total[5m]) == 0
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "No tasks being processed"
          description: "No tasks completed in queue {{ $labels.queue }} for 10 minutes"
```

## Best Practices

1. **Use metric labels wisely**: Labels like `queue` and `backend` help segment metrics, but avoid high-cardinality labels like `task_id`.

2. **Monitor queue depth**: Set up alerts for queue backlog to detect processing bottlenecks.

3. **Track duration percentiles**: Use histograms to understand task performance distribution, not just averages.

4. **Monitor failure rates**: High failure rates indicate issues with task execution or dependencies.

5. **Scrape interval**: Balance between metric freshness and overhead. 15-30 seconds is typical.

6. **Retention**: Configure Prometheus retention based on your needs. Default is 15 days.

## Troubleshooting

### Metrics not appearing

1. Verify `prometheus-client` is installed:
   ```bash
   pip show prometheus-client
   ```

2. Check Django logs for metrics initialization:
   ```
   INFO django_tasks_redis.metrics: TaskMetricsCollector initialized for backend: default
   INFO django_tasks_redis.metrics.signals: Metrics signal handlers registered
   ```

3. Ensure `ENABLE_METRICS` is set to `True` in backend options.

### Metrics endpoint returns 404

Verify your URL configuration includes the metrics endpoint.

### High memory usage

Prometheus metrics use memory for cardinality. If you have many queues or backends, memory usage will increase proportionally.

## Performance Impact

The metrics integration is designed to be lightweight:

- Signal handlers use non-blocking updates
- Errors in metrics collection are logged but don't affect task execution
- Histogram buckets are pre-allocated
- No external API calls

Expected overhead: < 1% CPU and < 50MB memory for typical workloads.
