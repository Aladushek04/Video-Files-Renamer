#!/usr/bin/env python3
"""Скрипт для переименования файлов из формата Video01.Lilly_PhillipsJenna.Ortega в нормальный вид."""

import os
import re
import sys
import argparse


def rename_file(src: str, dst: str) -> None:
    """Переименовывает файл src в dst."""
    os.rename(src, dst)


def parse_name(name: str) -> str:
    """
    Преобразует имя вида 'Lilly_PhillipsJenna.Ortega' в 'Lilly Phillips & Jenna Ortega'.
    Предполагаем, что каждое имя состоит из двух слов: имя и фамилия.
    Разделитель между людьми - это подстрока без разделения между фамилией и именем.
    """
    # Паттерн: несколько имён, разделённых подстроениями без пробелов
    # Формат: Имя1_Фамилица1Имя2.Фамилица2
    # Пример: Lilly_PhillipsJenna.Ortega
    
    # Разбиваем по точке (разделитель между людьми)
    parts = name.split('.')
    if len(parts) < 2:
        return name
    
    # Собираем пары имён
    people = []
    for part in parts:
        # Проверяем, является ли часть именем (без нижних подчеркиваний)
        words = part.split('_')
        if len(words) == 2:
            people.append(f"{words[0]} {words[1]}")
    
    if not people:
        return name
    
    # Соединяем с помощью ' & '
    return ' & '.join(people)


def rename_files(directory: str, dry_run: bool = True) -> None:
    """Переименовывает все файлы в директории."""
    for filename in os.listdir(directory):
        # Проверяем, является ли файл аудиофайлом
        if not filename.lower().endswith(('.mp3', '.wav', '.ogg', '.flac', '.wma', '.aiff', '.mid')):
            continue
        
        # Получаем путь к файлу
        file_path = os.path.join(directory, filename)
        
        # Получаем имя файла без расширения
        name_without_ext = os.path.splitext(filename)[0]
        
        # Проверяем, содержит ли имя точку (разделитель)
        if '.' not in name_without_ext:
            continue
        
        # Ищем паттерн 'Video01.Lilly_PhillipsJenna.Ortega'
        match = re.search(r'^(Video\d+)\.(.+)$', name_without_ext, re.IGNORECASE)
        if not match:
            continue
        
        prefix = match.group(1)
        names = match.group(2)
        
        # Преобразуем имена
        parsed_names = parse_name(names)
        
        # Формируем новое имя
        new_name = f"{prefix} - {parsed_names}"
        
        # Создаём новый путь
        new_full_path = os.path.join(directory, f"{new_name}{os.path.splitext(filename)[1]}")
        
        # Выводим результат
        if dry_run:
            print(f"  {filename} -> {os.path.basename(new_full_path)}")
        else:
            rename_file(file_path, new_full_path)


def main():
    parser = argparse.ArgumentParser(description='Переименовывает файлы из формата Video01.Lilly_PhillipsJenna.Ortega в нормальный вид.')
    parser.add_argument('directory', help='Директория с файлами для переименования')
    parser.add_argument('-n', '--dry-run', action='store_true', help='Показать результаты без фактического переименования')
    args = parser.parse_args()
    
    directory = args.directory
    if not os.path.isdir(directory):
        print(f"Ошибка: '{directory}' - каталог не найен.")
        sys.exit(1)
    
    print(f"Выполняется переименование файлов в '{directory}'...")
    rename_files(directory, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
</new_text>