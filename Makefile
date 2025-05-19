.PHONY: start stop down restart clean reset logs


start:
	docker compose up --build -d

stop:
	docker compose stop

down:
	docker compose down -v

restart:
	docker compose restart

clean:
	rm -rf ./pg_vol

reset: down clean

logs:
	docker compose logs -f
