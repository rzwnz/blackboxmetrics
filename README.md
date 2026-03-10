# Black Box Metrics — мониторинг air-gapped серверов Astra Linux

[![CI](https://github.com/rzwnz/blackboxmetrics/actions/workflows/ci.yml/badge.svg)](https://github.com/rzwnz/blackboxmetrics/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![VictoriaMetrics](https://img.shields.io/badge/VictoriaMetrics-v1.106.1-621773)
![Grafana](https://img.shields.io/badge/Grafana-11.4.0-F46800?logo=grafana&logoColor=white)
![Docker Compose](https://img.shields.io/badge/Docker_Compose-2.x-2496ED?logo=docker&logoColor=white)

---

## Оглавление

1. [Назначение](#1-назначение)
2. [Архитектура](#2-архитектура)
3. [Структура проекта](#3-структура-проекта)
4. [Требования](#4-требования)
5. [Быстрый запуск](#5-быстрый-запуск)
6. [Компоненты](#6-компоненты)
7. [Метрики](#7-метрики)
8. [Алертинг](#8-алертинг)
9. [Grafana-дашборды](#9-grafana-дашборды)
10. [Скрипты экспорта/импорта](#10-скрипты-экспортаимпорта)
11. [Переменные окружения](#11-переменные-окружения)
12. [Тестирование](#12-тестирование)
13. [Частые проблемы](#13-частые-проблемы)

---

## 1. Назначение

Система «чёрного ящика» для мониторинга серверов **Astra Linux** без прямого сетевого доступа. Метрики собираются на изолированном сервере, экспортируются в архив и переносятся на рабочую станцию разработчика для офлайн-анализа в Grafana.

**Два стека Docker Compose:**
- **Серверный** (`docker-compose.server.yml`) — собирает метрики на air-gapped сервере
- **Аналитический** (`docker-compose.analysis.yml`) — импортирует и визуализирует данные на рабочей станции

---

## 2. Архитектура

```
┌────────────────────────────────────────────────────────────────┐
│                    Air-gapped сервер Astra Linux               │
│                                                                │
│  ┌──────────────────────┐    ┌──────────────┐                  │
│  │  VictoriaMetrics     │◄───┤ promscrape   │                  │
│  │  :8428 (TSDB, 90d)   │    │ (prometheus. │                  │
│  └──────────┬───────────┘    │  yml)        │                  │
│             │                └───────┬──────┘                  │
│  ┌──────────▼───────────┐          ┌─┼──────────────────┐      │
│  │  vmalert  :8880      │          │ │  Scrape targets: │      │
│  │  (16 alert rules)    │          │ ├─ node-exporter   │      │
│  └──────────────────────┘          │ │  :9100           │      │
│                                    │ ├─ s3-exporter     │      │
│  ┌──────────────────────┐          │ │  :9340           │      │
│  │  dump-metrics.sh     │───────►  │ └─ tomcat-exporter │      │
│  │  (экспорт JSONL +    │ tar.gz   │    :9341           │      │
│  │   логи + SHA-256)    │          └────────────────────┘      │
│  └──────────────────────┘                                      │
└───────────── Перенос на USB / SCP ─────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                  Рабочая станция разработчика                    │
│                                                                  │
│  ┌──────────────────────┐    ┌────────────────┐                  │
│  │  import-metrics.sh   │───►│ VictoriaMetrics│                  │
│  │  (импорт JSONL,      │    │  :8428 (365d)  │                  │
│  │   проверка SHA-256)  │    └─────────┬──────┘                  │
│  └──────────────────────┘              │                         │
│                                ┌───────▼───────┐                 │
│                                │   Grafana     │                 │
│                                │   :3000       │                 │
│                                │  (3 дашборда) │                 │
│                                └───────────────┘                 │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Структура проекта

```
blackboxmetrics/
├── docker-compose.server.yml        # Стек для air-gapped сервера
├── docker-compose.analysis.yml      # Стек для офлайн-анализа
├── alerting/
│   └── alert-rules.yml              # 16 правил алертинга (3 группы)
├── exporters/
│   ├── s3-exporter/                 # Python-экспортёр S3/Garage (7 метрик)
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── s3_exporter.py
│   └── tomcat-exporter/             # Python-экспортёр Tomcat (20 метрик)
│       ├── Dockerfile
│       ├── requirements.txt
│       └── tomcat_exporter.py
├── grafana/
│   ├── dashboards/
│   │   ├── s3-metrics.json          # Дашборд S3-хранилища
│   │   ├── system-overview.json     # Системный обзор (CPU, RAM, Disk)
│   │   └── tomcat-metrics.json      # Дашборд Tomcat
│   └── provisioning/
│       ├── dashboards/dashboards.yml
│       └── datasources/victoriametrics.yml
├── prometheus/
│   └── prometheus.yml               # Конфигурация scrape targets
├── scripts/
│   ├── dump-metrics.sh              # Экспорт метрик + логов в архив
│   └── import-metrics.sh            # Импорт архива на рабочую станцию
└── tests/
    └── test_exporters.py            # Unit-тесты экспортёров (pytest)
```

---

## 4. Требования

- **Docker** >= 24.0, **Docker Compose** >= 2.20
- **Python 3.12** (для разработки экспортёров)
- **bash** (для скриптов экспорта/импорта)
- Опционально: **pigz** (ускоренное сжатие архивов)

---

## 5. Быстрый запуск

### Серверный стек (на air-gapped машине)

```bash
# Задать переменные S3 и Tomcat
export S3_ENDPOINT=http://garage:3900
export S3_ACCESS_KEY=... S3_SECRET_KEY=...
export TOMCAT_URL=http://tomcat:8080

docker compose -f docker-compose.server.yml up -d
```

Проверка:
```bash
curl http://localhost:8428/api/v1/targets    # targets VictoriaMetrics
curl http://localhost:9340/metrics            # метрики S3
curl http://localhost:9341/metrics            # метрики Tomcat
curl http://localhost:9100/metrics            # метрики node-exporter
curl http://localhost:8880/api/v1/alerts      # активные алерты
```

### Аналитический стек (на рабочей станции)

```bash
docker compose -f docker-compose.analysis.yml up -d

# Импорт данных с сервера
./scripts/import-metrics.sh /path/to/blackbox-dump-20250101_120000.tar.gz
```

Grafana: [http://localhost:3000](http://localhost:3000) (логин/пароль: `admin` / `admin`).

---

## 6. Компоненты

### Серверный стек (`docker-compose.server.yml`)

| Сервис | Образ | Порт | Описание |
|--------|-------|------|----------|
| `victoriametrics` | `victoriametrics/victoria-metrics:v1.106.1` | 8428 | TSDB, retention 90 дней, promscrape внутри |
| `node-exporter` | `prom/node-exporter:v1.8.2` | 9100 | Системные метрики (CPU, RAM, диск, сеть) |
| `s3-exporter` | build `./exporters/s3-exporter` | 9340 | S3/Garage метрики (Python 3.12) |
| `tomcat-exporter` | build `./exporters/tomcat-exporter` | 9341 | Tomcat JVM/connector метрики (Python 3.12) |
| `vmalert` | `victoriametrics/vmalert:v1.106.1` | 8880 | Вычисление правил алертинга (интервал 1 мин) |

### Аналитический стек (`docker-compose.analysis.yml`)

| Сервис | Образ | Порт | Описание |
|--------|-------|------|----------|
| `victoriametrics` | `victoriametrics/victoria-metrics:v1.106.1` | 8428 | Локальная TSDB, retention 365 дней |
| `grafana` | `grafana/grafana:11.4.0` | 3000 | Визуализация, 3 преднастроенных дашборда |

---

## 7. Метрики

### S3-экспортёр (7 метрик)

| Метрика | Тип | Labels | Описание |
|---------|-----|--------|----------|
| `s3_bucket_objects_total` | Gauge | `bucket` | Количество объектов |
| `s3_bucket_size_bytes` | Gauge | `bucket` | Суммарный размер объектов |
| `s3_bucket_largest_object_bytes` | Gauge | `bucket` | Размер наибольшего объекта |
| `s3_bucket_up` | Gauge | `bucket` | Доступность бакета (1/0) |
| `s3_exporter_scrape_errors_total` | Counter | — | Число ошибок scrape |
| `s3_exporter_scrape_duration_seconds` | Gauge | — | Длительность последнего scrape |
| `s3_exporter_info` | Info | — | Версия, endpoint |

### Tomcat-экспортёр (20 метрик)

| Метрика | Тип | Labels | Описание |
|---------|-----|--------|----------|
| `tomcat_jvm_memory_free_bytes` | Gauge | — | Свободная память JVM |
| `tomcat_jvm_memory_total_bytes` | Gauge | — | Общая память JVM |
| `tomcat_jvm_memory_max_bytes` | Gauge | — | Максимальная память JVM |
| `tomcat_memory_pool_usage_bytes` | Gauge | `pool`, `type` | Утилизация пула памяти |
| `tomcat_memory_pool_max_bytes` | Gauge | `pool`, `type` | Максимум пула памяти |
| `tomcat_connector_thread_max` | Gauge | `connector` | Макс. потоков коннектора |
| `tomcat_connector_thread_count` | Gauge | `connector` | Текущее число потоков |
| `tomcat_connector_thread_busy` | Gauge | `connector` | Занятые потоки |
| `tomcat_connector_max_connections` | Gauge | `connector` | Макс. соединений |
| `tomcat_connector_connection_count` | Gauge | `connector` | Текущие соединения |
| `tomcat_connector_request_count_total` | Gauge | `connector` | Обработано запросов |
| `tomcat_connector_error_count_total` | Gauge | `connector` | Ошибки |
| `tomcat_connector_bytes_received_total` | Gauge | `connector` | Получено байт |
| `tomcat_connector_bytes_sent_total` | Gauge | `connector` | Отправлено байт |
| `tomcat_connector_processing_time_ms_total` | Gauge | `connector` | Суммарное время обработки (мс) |
| `tomcat_connector_max_time_ms` | Gauge | `connector` | Макс. время одного запроса (мс) |
| `tomcat_up` | Gauge | — | Доступность Tomcat (1/0) |
| `tomcat_exporter_scrape_errors_total` | Counter | — | Число ошибок scrape |
| `tomcat_exporter_scrape_duration_seconds` | Gauge | — | Длительность последнего scrape |
| `tomcat_exporter_info` | Info | — | Версия, target URL |

---

## 8. Алертинг

16 правил, 3 группы. Правила обрабатываются `vmalert` и записываются как аннотации в VictoriaMetrics.

### Группа `system` (интервал 1 мин)

| Алерт | Условие | Severity |
|-------|---------|----------|
| `HighCpuUsage` | CPU > 90% в течение 5 мин | warning |
| `CriticalCpuUsage` | CPU > 95% в течение 10 мин | critical |
| `HighMemoryUsage` | RAM > 85% в течение 5 мин | warning |
| `CriticalMemoryUsage` | RAM > 95% в течение 5 мин | critical |
| `DiskSpaceLow` | Root FS > 85% в течение 5 мин | warning |
| `DiskSpaceCritical` | Root FS > 95% в течение 5 мин | critical |
| `HighDiskIOWait` | iowait > 15% в течение 10 мин | warning |
| `HighLoadAverage` | load15 > 2× ядер CPU в течение 15 мин | warning |

### Группа `s3` (интервал 2 мин)

| Алерт | Условие | Severity |
|-------|---------|----------|
| `S3BucketDown` | Бакет недоступен 3 мин | critical |
| `S3BucketSizeTooLarge` | Бакет > 50 ГБ в течение 5 мин | warning |
| `S3ExporterErrors` | Ошибки scrape 10 мин | warning |

### Группа `tomcat` (интервал 1 мин)

| Алерт | Условие | Severity |
|-------|---------|----------|
| `TomcatDown` | Tomcat недоступен 2 мин | critical |
| `TomcatHighThreadUsage` | > 85% пула потоков занято 5 мин | warning |
| `TomcatHighErrorRate` | > 5% ошибок в течение 5 мин | warning |
| `TomcatHighJvmMemory` | JVM heap > 90% в течение 10 мин | warning |
| `TomcatSlowRequests` | Средняя задержка > 2 с в течение 5 мин | warning |

---

## 9. Grafana-дашборды

| Дашборд | Описание |
|---------|----------|
| **System Overview** | CPU, RAM, диск, сеть, load average, iowait |
| **S3 Metrics** | Объекты, размер, доступность бакетов, ошибки scrape |
| **Tomcat Metrics** | JVM-память, потоки, соединения, запросы, ошибки, задержки |

Дашборды автоматически провизионятся через `grafana/provisioning/` при запуске контейнера.

---

## 10. Скрипты экспорта/импорта

### `dump-metrics.sh` — экспорт с сервера

```bash
./scripts/dump-metrics.sh
```

Выполняет:
1. Проверяет наличие `curl`, `tar`, `sha256sum`
2. Экспортирует метрики из VictoriaMetrics (`/api/v1/export`, формат JSONL) за указанный период (по умолчанию — последние 24 часа)
3. Собирает логи из директории `LOGS_DIR` (по умолчанию `/var/log/tandem`)
4. Добавляет метаданные (hostname, время, список метрик)
5. Создаёт архив `blackbox-dump-<timestamp>.tar.gz` + файл контрольной суммы SHA-256

Переменные: `VM_URL`, `LOGS_DIR`, `OUTPUT_DIR`, `DUMP_PREFIX`.

### `import-metrics.sh` — импорт на рабочую станцию

```bash
./scripts/import-metrics.sh /path/to/blackbox-dump-*.tar.gz
```

Выполняет:
1. Проверяет SHA-256 контрольную сумму (если файл `.sha256` присутствует)
2. Распаковывает архив
3. Импортирует JSONL-метрики в локальный VictoriaMetrics (`/api/v1/import`)
4. Копирует логи в `./import/logs/`

---

## 11. Переменные окружения

### S3-экспортёр

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `S3_ENDPOINT` | — | URL S3-совместимого хранилища |
| `S3_ACCESS_KEY` | — | Ключ доступа |
| `S3_SECRET_KEY` | — | Секретный ключ |
| `S3_BUCKET` | `*` | Имена бакетов (через запятую) или `*` для всех |
| `S3_REGION` | `garage` | Регион |
| `SCRAPE_INTERVAL` | `60` | Интервал сбора (секунды) |
| `EXPORTER_PORT` | `9340` | Порт экспортёра |

### Tomcat-экспортёр

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `TOMCAT_URL` | — | URL сервера Tomcat |
| `TOMCAT_STATUS_PATH` | `/status` | Путь к status-эндпоинту |
| `TOMCAT_USER` | — | Логин (Basic Auth, опционально) |
| `TOMCAT_PASSWORD` | — | Пароль (Basic Auth, опционально) |
| `SCRAPE_INTERVAL` | `30` | Интервал сбора (секунды) |
| `EXPORTER_PORT` | `9341` | Порт экспортёра |

---

## 12. Тестирование

```bash
cd tests
pip install -r ../exporters/s3-exporter/requirements.txt
pip install -r ../exporters/tomcat-exporter/requirements.txt
pytest test_exporters.py -v
```

Тесты:
- **test_tomcat_parse_status_updates_metrics** — парсинг XML-ответа Tomcat, проверка значений JVM-памяти и метрик коннектора
- **test_s3_collect_bucket_metrics_updates_gauges** — mock S3-клиент, проверка количества объектов, размера, наибольшего объекта и gauge доступности

---

## 13. Частые проблемы

| Проблема | Решение |
|----------|---------|
| Порт 8428 занят | `docker ps` → остановить конфликтующий контейнер или изменить порт в compose |
| S3-экспортёр не собирает метрики | Проверить `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`; убедиться что Garage/MinIO доступен |
| Tomcat-экспортёр — `tomcat_up` = 0 | Проверить `TOMCAT_URL`, убедиться что status-эндпоинт доступен (`/status?XML=true`) |
| После импорта данные не отображаются | Убедиться что аналитический стек запущен; проверить time range в Grafana |
| SHA-256 не совпадает при импорте | Повторить копирование архива, проверить целостность носителя |
| Grafana показывает «No Data» | Проверить datasource (VictoriaMetrics :8428), убедиться что метрики импортированы |
