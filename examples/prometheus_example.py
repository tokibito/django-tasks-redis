"""
Example showing how to use Prometheus metrics with django-tasks-redis.

This example demonstrates:
1. Enabling metrics in settings
2. Exposing a metrics endpoint
3. Monitoring task execution

The metrics collector queries Redis directly when Prometheus scrapes the endpoint,
ensuring accurate metrics regardless of which process serves the endpoint or processes tasks.
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
    '''
    Expose Prometheus metrics endpoint.

    The django-tasks-redis collector automatically queries Redis for current
    task statistics when this endpoint is scraped, so metrics are always
    up-to-date even if this process doesn't execute any tasks.
    '''
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

# When Prometheus scrapes /metrics, it will show current queue state from Redis:
# - django_tasks_queue_length{backend="default",status="READY"} 2
# - django_tasks_queue_length{backend="default",status="RUNNING"} 0
# - django_tasks_queue_length{backend="default",status="SUCCESSFUL"} 0
# - django_tasks_queue_length{backend="default",status="FAILED"} 0
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

## Current Queue Metrics

# Pending tasks waiting to be processed
django_tasks_queue_length{status="READY"}

# Currently running tasks
django_tasks_queue_length{status="RUNNING"}

# Successfully completed tasks (historical count)
django_tasks_queue_length{status="SUCCESSFUL"}

# Failed tasks (historical count)
django_tasks_queue_length{status="FAILED"}

## Total tasks in system
sum(django_tasks_queue_length)

## Tasks by status (stacked graph)
sum by (status) (django_tasks_queue_length)

## Success rate (requires SUCCESSFUL and FAILED counts)
django_tasks_queue_length{status="SUCCESSFUL"} /
(django_tasks_queue_length{status="SUCCESSFUL"} + django_tasks_queue_length{status="FAILED"})
* 100
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
          description: "Backend {{ $labels.backend }} has {{ $value }} pending tasks"

      - alert: TasksStuckInRunningState
        expr: django_tasks_queue_length{status="RUNNING"} > 100
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Many tasks stuck in RUNNING state"
          description: "Backend {{ $labels.backend }} has {{ $value }} tasks in RUNNING state for >10min"

      - alert: HighFailureCount
        expr: django_tasks_queue_length{status="FAILED"} > 500
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "High number of failed tasks"
          description: "Backend {{ $labels.backend }} has {{ $value }} failed tasks"
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
