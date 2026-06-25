# CTFd — локальный Kind кластер

Продакшн-подобный локальный стенд для разработки плагинов CTFd.  
Без Bitnami. Полностью IaC: Kustomize + Helm (операторы) + Makefile.

## Архитектура

```
браузер → http://ctf.school.local
              ↓
      Envoy Gateway (LoadBalancer)
         ← cloud-provider-kind →
              ↓ HTTPRoute
         CTFd (2+ подов, HPA)
           ↙            ↘
  MariaDB Galera (3)   KeyDB (2, active-replica)
```

| Слой           | Решение                      | Почему                                   |
|----------------|------------------------------|------------------------------------------|
| Трафик         | Envoy Gateway + Gateway API  | современный стандарт, заменяет Ingress   |
| LoadBalancer   | cloud-provider-kind          | реальные IP без хаков с портами          |
| База данных    | mariadb-operator → Galera    | HA без Bitnami                           |
| Кэш / очередь | KeyDB (active-replica)       | Redis-совместимый, многопоточный         |
| TLS            | cert-manager (pre-installed) | готов к подключению сертификата          |

## Требования

```bash
brew install kind kubectl helm
go install sigs.k8s.io/cloud-provider-kind@latest
```

Версии Helm-чартов (можно переопределить через `make VAR=значение`):

| Переменная        | По умолчанию | Как проверить актуальную                          |
|-------------------|--------------|---------------------------------------------------|
| `CERT_MANAGER_VER`| `v1.17.2`    | `helm search repo jetstack/cert-manager`          |
| `ENVOY_GW_VER`    | `v1.4.1`     | `helm search repo envoy-gateway` (OCI)            |
| `MARIADB_OP_VER`  | `26.6.0`     | `helm search repo mariadb-operator/mariadb-operator` |

## Быстрый старт

```bash
# Терминал 1 — держать открытым всё время работы кластера
sudo cloud-provider-kind

# Терминал 2
cd ctf-school/ctfd
make all
```

`make all` выполняет шаги по порядку:

| Шаг | Таргет         | Что делает                                          |
|-----|----------------|-----------------------------------------------------|
| 1   | `make cluster` | создаёт Kind кластер (4 ноды)                       |
| 2   | `make infra`   | cert-manager, Envoy Gateway, mariadb-operator       |
| 3   | `make data`    | MariaDB Galera CR + KeyDB StatefulSet               |
| 4   | `make build`   | `docker build` образа с плагином                    |
| 5   | `make load`    | загружает образ в Kind                              |
| 6   | `make app`     | деплоит CTFd (Deployment, Service, HPA, PDB)        |
| 7   | `make gateway` | GatewayClass, Gateway, HTTPRoute                    |
| 8   | `make hosts`   | прописывает `ctf.school.local` в `/etc/hosts`       |

После завершения открывай **http://ctf.school.local**.

## Разработка плагина

При изменении кода в `plugin/`:

```bash
make dev
# = docker build → kind load → kubectl rollout restart
```

Rolling restart без даунтайма — занимает ~30 секунд.

## Полезные команды

```bash
make ip       # IP-адрес Gateway
make hosts    # обновить /etc/hosts (идемпотентно)
make logs     # стриминг логов всех CTFd подов
make destroy  # удалить кластер
```

```bash
# Статус Galera-кластера
kubectl -n ctfd exec -it ctfd-db-0 -- \
  mariadb -uctfd -pctfd-secret -e "SHOW STATUS LIKE 'wsrep_cluster_size';"

# Статус репликации KeyDB
kubectl -n ctfd exec -it keydb-0 -- keydb-cli info replication
kubectl -n ctfd exec -it keydb-1 -- keydb-cli info replication

# Все поды с распределением по нодам
kubectl -n ctfd get pods -o wide
```

## Структура IaC

```
ctfd/
├── kind.yaml                  # Kind кластер (1 CP + 3 workers)
├── Makefile                   # единая точка входа
├── Dockerfile                 # кастомный CTFd с плагином
├── plugin/                    # плагин lab_manager
└── k8s/
    ├── mariadb/               # Secret + MariaDB CR (Galera)
    ├── keydb/                 # StatefulSet + Services
    ├── ctfd/                  # Secret, Deployment, Service, HPA, PDB
    └── gateway/               # GatewayClass, Gateway, HTTPRoute
```

Операторы (cert-manager, envoy-gateway, mariadb-operator) — Helm в `make infra`.  
Всё остальное — `kubectl apply -k` (Kustomize).

## Нюансы

**MariaDB Galera** при первом старте занимает 3–5 минут: оператор поднимает bootstrap-ноду, потом добавляет joiner'ы. `make data` ждёт автоматически.

**cloud-provider-kind** должен быть запущен пока работает кластер. Если упал — перезапустить, через ~30 секунд Gateway снова получит IP. После этого перезапустить `make hosts` чтобы обновить `/etc/hosts`.

**`make hosts`** идемпотентен: сначала удаляет старую строку с доменом из `/etc/hosts`, затем добавляет актуальную. Можно запускать сколько угодно раз.

**HTTPS**: listener уже объявлен в `k8s/gateway/gateway.yaml`. Добавить Secret `ctfd-tls` с сертификатом (или `Certificate` CR от cert-manager) — и он активируется без изменения остальных манифестов.
