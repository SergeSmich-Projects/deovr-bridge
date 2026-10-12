#!/usr/bin/env python3
"""
Тестовый скрипт для проверки нового matcher.py
"""
import time
import matcher
import store

def main():
    print("=== Тест нового matcher.py ===")
    
    # 1. Получаем все локальные карточки
    print("\n1. Загрузка локальных карточек...")
    start = time.time()
    all_items = store.all_items()
    local_items = [i for i in all_items if i.get('site') in ('pornolab', 'ptorrents')]
    print(f"   Найдено {len(local_items)} локальных карточек за {time.time() - start:.2f} сек")
    
    # 2. Инициализируем matcher
    print("\n2. Инициализация matcher...")
    start = time.time()
    # Здесь нужно создать экземпляр PlayaMatcher
    # Но сначала нужно загрузить каталог API
    print("   TODO: реализовать загрузку каталога API")
    
    # 3. Тестовое сопоставление
    print("\n3. Тестовое сопоставление...")
    if local_items:
        test_item = local_items[0]
        title = test_item.get('title', '')
        print(f"   Тестовая карточка: {title[:50]}...")
        print("   TODO: реализовать сопоставление")
    
    print("\n=== Тест завершён ===")

if __name__ == "__main__":
    main()
