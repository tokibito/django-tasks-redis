"""
Example showing how to use Prometheus metrics with django-tasks-redis.

This example demonstrates:
1. Enabling metrics in settings
2. Exposing a metrics endpoint
3. Monitoring task execution
"""

# Example Django settings configuration
EXAMPLE_SETTINGS = """
# settings.py

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_tasks_redis',
]

TASKS = {
    'default': {
        'BACKEND': 'django_tasks_redis.RedisTaskBackend',
        'QUEUES': [],
        'OPTIONS': {
            'REDIS_URL': 'redis://localhost:6379/0',
            'ENABLE_METRICS': True,  # Enable Prometheus metrics
        },
    },
}
"""

# Example views.py for exposing metrics endpoint
EXAMPLE_VIEWS = """
# myapp/views.py

from django.http import HttpResponse
from prometheus_client import REGISTRY, generate_latest


def metrics_view(request):
    '''Expose Prometheus metrics endpoint.'''
    metrics = generate_latest(REGISTRY)
    return HttpResponse(
        metrics,
        content_type='text/plain; charset=utf-8'
    )
"""

# Example urls.py
EXAMPLE_URLS = """
# urls.py

from django.urls import path, include
from myapp.views import metrics_view

urlpatterns = [
    # Your other URLs
    path('admin/', admin.site.urls),

    # Task execution endpoints
    path('tasks/', include('django_tasks_redis.urls')),

    # Prometheus metrics endpoint
    path('metrics/', metrics_view, name='metrics'),
]
"""

# Example task definition
EXAMPLE_TASK = """
# tasks.py

from django.tasks import task
import time


@task
def process_data(data_id: int):
    '''Example task that processes data.'''
    # Simulate processing
    time.sleep(2)

    # Do actual work
    result = f"Processed data {data_id}"

    return result


@task(queue_name="emails")
def send_notification(email: str, message: str):
    '''Example task for sending emails.'''
    # Send email logic here
    print(f"Sending to {email}: {message}")
    return True
"""

# Example of enqueueing tasks
EXAMPLE_ENQUEUE = """
# Example: Enqueueing tasks

from myapp.tasks import process_data, send_notification

# Enqueue tasks
result1 = process_data.enqueue(data_id=123)
result2 = send_notification.enqueue(
    email="user@example.com",
    message="Hello!"
)

print(f"Task 1 ID: {result1.id}")
print(f"Task 2 ID: {result2.id}")

# Metrics will automatically track:
# - django_tasks_enqueued_total (incremented)
# - django_tasks_queue_length{status="READY"} (updated)
"""

# Example Prometheus scrape config
EXAMPLE_PROMETHEUS_CONFIG = """
# prometheus.yml

global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: 'django-tasks'
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/metrics'
    scrape_interval: 15s
"""

# Example PromQL queries for Grafana
EXAMPLE_PROMQL_QUERIES = """
# Grafana Dashboard Queries

## Queue Backlog (Gauge)
django_tasks_queue_length{status="READY"}

## Task Throughput (Graph)
rate(django_tasks_enqueued_total[1m])
rate(django_tasks_completed_total[1m])
rate(django_tasks_failed_total[1m])

## Task Duration - 95th Percentile (Graph)
histogram_quantile(0.95,
  rate(django_tasks_duration_seconds_bucket[5m])
)

## Success Rate (Gauge)
(
  rate(django_tasks_completed_total[5m]) /
  (rate(django_tasks_completed_total[5m]) + rate(django_tasks_failed_total[5m]))
) * 100

## Tasks by Queue (Table)
sum by (queue) (django_tasks_queue_length)
"""

# Example alert rules
EXAMPLE_ALERT_RULES = """
# prometheus-alerts.yml

groups:
  - name: django_tasks_alerts
    rules:
      - alert: HighTaskQueueBacklog
        expr: django_tasks_queue_length{status="READY"} > 1000
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High task queue backlog detected"
          description: "Queue {{ $labels.queue }} has {{ $value }} pending tasks"

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
          description: "More than 10% of tasks are failing in queue {{ $labels.queue }}"
"""


def main():
    """Print example configurations."""
    print("=" * 80)
    print("Django Tasks Redis - Prometheus Integration Examples")
    print("=" * 80)

    print("\n## 1. Django Settings Configuration")
    print(EXAMPLE_SETTINGS)

    print("\n## 2. Metrics Endpoint View")
    print(EXAMPLE_VIEWS)

    print("\n## 3. URL Configuration")
    print(EXAMPLE_URLS)

    print("\n## 4. Task Definition")
    print(EXAMPLE_TASK)

    print("\n## 5. Enqueueing Tasks")
    print(EXAMPLE_ENQUEUE)

    print("\n## 6. Prometheus Configuration")
    print(EXAMPLE_PROMETHEUS_CONFIG)

    print("\n## 7. PromQL Queries for Grafana")
    print(EXAMPLE_PROMQL_QUERIES)

    print("\n## 8. Alert Rules")
    print(EXAMPLE_ALERT_RULES)

    print("\n" + "=" * 80)
    print("For complete documentation, see PROMETHEUS.md")
    print("=" * 80)


if __name__ == "__main__":
    main()
