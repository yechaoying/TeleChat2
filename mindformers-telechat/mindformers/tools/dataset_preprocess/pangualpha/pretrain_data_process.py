# Copyright 2023 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

"""
transform wikitext-2, wikitext-103, lambada, openwebtext dataset to mindrecord.
"""
from __future__ import (absolute_import, division, print_function, unicode_literals)

import argparse
import glob
import json
import os
import re
import time
from multiprocessing import current_process, Process
import numpy as np

import sentencepiece as spm
import jieba

from mindspore.mindrecord import FileWriter


def chunks(lst, n):
    """ yield n sized chunks from list"""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def package_file(it, n):
    """ package multiple files"""
    stop = False
    while not stop:
        batch = []
        for _ in range(n):
            try:
                batch.append(next(it))
            except StopIteration:
                stop = True
        if not batch:
            break
        yield batch


def clean_wikitext(string):
    """ cleaning wikitext dataset"""
    # contractions
    string = string.replace("s '", "s'")
    string = re.sub(r"/' [0-9]/", r"/'[0-9]/", string)
    # number separators
    string = string.replace(" @-@ ", "-")
    string = string.replace(" @,@ ", ",")
    string = string.replace(" @.@ ", ".")
    # punctuation
    string = string.replace(" : ", ": ")
    string = string.replace(" ; ", "; ")
    string = string.replace(" . ", ". ")
    string = string.replace(" ! ", "! ")
    string = string.replace(" ? ", "? ")
    string = string.replace(" , ", ", ")
    # double brackets
    string = re.sub(r"\(\s*([^\)]*?)\s*\)", r"(\1)", string)
    string = re.sub(r"\[\s*([^\]]*?)\s*\]", r"[\1]", string)
    string = re.sub(r"{\s*([^}]*?)\s*}", r"{\1}", string)
    string = re.sub(r"\"\s*([^\"]*?)\s*\"", r'"\1"', string)
    string = re.sub(r"'\s*([^']*?)\s*'", r"'\1'", string)
    # miscellaneous
    string = string.replace("= = = =", "====")
    string = string.replace("= = =", "===")
    string = string.replace("= =", "==")
    string = string.replace(" " + chr(176) + " ", chr(176))
    string = string.replace(" \n", "\n")
    string = string.replace("\n ", "\n")
    string = string.replace(" N ", " 1 ")
    string = string.replace(" 's", "'s")
    return string


def tokenize_openwebtext(tokenizer, iterator, seq_length, eot):
    """ tokenize openwebtext dataset"""
    for file_path in iterator:
        if os.path.getsize(file_path) == 0:
            continue
        content = []
        with open(file_path, 'r', encoding='utf-8') as f:
            for para in f.read().split("\n\n"):
                if para:
                    tokenized_text = tokenizer.tokenize(para)
                    content += tokenizer.convert_tokens_to_ids(tokenized_text) + [
                        eot]
        for chunk in chunks(content, seq_length):
            sample = {}
            if len(chunk) == seq_length:
                sample['input_ids'] = np.array(chunk, dtype=np.int32)
                yield sample


def tokenize_wiki(tokenizer, file_path, seq_length, eot):
    """tokenize wikitext-2/wikitext-103 dataset"""
    content = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for para in clean_wikitext(f.read()).split("\n\n"):
            if para and para.strip().startswith('=') is False:
                tokenized_text = tokenizer.tokenize(para)
                content += tokenizer.convert_tokens_to_ids(tokenized_text) + [
                    eot]
    for chunk in chunks(content, seq_length):
        sample = {}
        if len(chunk) == seq_length:
            sample['input_ids'] = np.array(chunk, dtype=np.int32)
            yield sample


def tokenize_lambada(tokenizer, file_path, seq_length, eot):
    """tokenize lambada dataset"""
    content = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f.readlines():
            para = json.loads(line)['text'].replace(
                "“", '"').replace("”", '"').strip().strip(".")
            tokenized_text = tokenizer.tokenize(para)
            content += tokenizer.convert_tokens_to_ids(tokenized_text) + [eot]
    for chunk in chunks(content, seq_length):
        sample = {}
        if len(chunk) == seq_length:
            sample['input_ids'] = np.array(chunk, dtype=np.int32)
            yield sample


def task_unit(iterator, tokenizer, seq_length, eot, mindrecord_filename, schema):
    """write data into mindrecord"""
    writer = FileWriter(file_name=mindrecord_filename, shard_num=1)
    writer.add_schema(schema, args.dataset_type)

    p = current_process()
    index = p.pid if p.pid else 0

    item_iter = tokenize_openwebtext(tokenizer, iterator, seq_length, eot)
    batch_size = 1024  # size of write batch
    count = 0
    while True:
        data_batch = []
        try:
            for _ in range(batch_size):
                data_batch.append(next(item_iter))
                count += 1
            writer.write_raw_data(data_batch)
            print("Process {} transformed {} records.".format(
                index, count))
        except StopIteration:
            if data_batch:
                writer.write_raw_data(data_batch)
                print("Process {} transformed {} records.".format(
                    index, count))
            break
    writer.commit()


class JIEBATokenizer():
    r"""
    Jieba Tokenizer
    """
    def __init__(self, model_file, max_len=None):
        self.max_len = max_len if max_len is not None else int(1e12)
        self.encoder = {}
        self.sp = spm.SentencePieceProcessor(model_file=model_file)

        for i in range(self.sp.get_piece_size()):
            self.encoder[self.sp.id_to_piece(i)] = i
        self.translator = str.maketrans(" \n", "\u2582\u2583")

        self.eod_id = self.encoder.get('<eod>')
        self.eot_id = self.encoder.get('<eot>')
        self.pad_id = self.encoder.get('<pad>')

    @property
    def vocab_size(self):
        return len(self.encoder)

    def __len__(self):
        return len(self.encoder) + len(self.special_tokens)

    @property
    def eod(self):
        return self.eod_id

    def tokenize(self, text):
        """ Tokenize a string. """
        seg_list = [x.translate(self.translator) for x in jieba.cut(text, cut_all=False)]
        return seg_list

    def convert_tokens_to_ids(self, tokens):
        new_seg = " ".join(tokens)
        return self.sp.encode(new_seg)

    def convert_ids_to_tokens(self, ids):
        return self.sp.id_to_piece(ids)

    def process_tokens(self, text):
        text = text.replace(' ', '').replace('\u2582', ' ').replace('\u2583', '\n')
        return text

    def encode(self, text):
        res = self.tokenize(text)
        return res

    def decode(self, tokens):
        text = self.sp.decode(tokens)
        return self.process_tokens(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset_type', type=str, default='openwebtext')
    parser.add_argument('--input_glob', type=str, default='*.txt')
    parser.add_argument('--output_file', type=str,
                        default='./output/transfered_mindrecord')
    parser.add_argument('--tokenizer', type=str, default='jieba', choices=['gpt', 'jieba'])
    parser.add_argument('--model_file', type=str, default=None)
    parser.add_argument('--file_partition', type=int, default=1)
    parser.add_argument('--file_batch_size', type=int, default=1024)
    parser.add_argument('--num_process', type=int, default=64)
    parser.add_argument('--seq_length', type=int, default=1025)
    parser.add_argument('--eot', type=int, default=3, help="Eod of text depends on the vocab file.")
    parser.add_argument('--data_column_name', type=str, default='input_ids')


    args = parser.parse_args()
    # pylint: disable=C0326
    out_dir, out_file = os.path.split(os.path.abspath(args.output_file))
    if not os.path.exists(out_dir):
        os.mkdir(out_dir)
    mindrecord_schema = {args.data_column_name: {"type": "int32", "shape": [-1]}, }

    # Start to load tokenizer
    if args.tokenizer == 'gpt':
        try:
            from transformers import GPT2Tokenizer
        except ModuleNotFoundError:
            print("module 'transformers' not installed.")
        word_tokenizer = GPT2Tokenizer.from_pretrained("gpt2")
    else:
        if not os.path.exists(args.model_file):
            raise FileNotFoundError(f"file {args.model_file} do not exists.")
        word_tokenizer = JIEBATokenizer(model_file=args.model_file)

    transforms_count = 0
    if args.dataset_type == 'wiki':
        wiki_writer = FileWriter(file_name=args.output_file, shard_num=args.file_partition)
        wiki_writer.add_schema(mindrecord_schema, args.dataset_type)
        for x in tokenize_wiki(word_tokenizer, args.input_glob, args.seq_length, args.eot):
            transforms_count += 1
            wiki_writer.write_raw_data([x])
        wiki_writer.commit()
        print("Transformed {} records.".format(transforms_count))
    elif args.dataset_type == 'lambada':
        lambada_writer = FileWriter(file_name=args.output_file, shard_num=args.file_partition)
        lambada_writer.add_schema(mindrecord_schema, args.dataset_type)
        for x in tokenize_lambada(word_tokenizer, args.input_glob, args.seq_length, args.eot):
            transforms_count += 1
            lambada_writer.write_raw_data([x])
        lambada_writer.commit()
        print("Transformed {} records.".format(transforms_count))
    elif args.dataset_type == 'openwebtext':
        SUFFIX = len(str(args.file_partition - 1))
        file_names = ["{}{}".format(args.output_file, str(x).rjust(SUFFIX, '0'))
                      for x in range(args.file_partition)]
        file_iter = glob.iglob(args.input_glob)
        process_list = {}
        for file in file_names:
            p1 = Process(target=task_unit, args=(file_iter, word_tokenizer, args.seq_length,
                                                 args.eot, file, mindrecord_schema))
            p1.start()
            process_list[file] = p1
        for process in process_list.values():
            while process.is_alive(): # wait child process exit
                time.sleep(0.01)
    else:
        raise ValueError(
            "Not support dataset type: {}".format(args.dataset_type))

    out_file = args.output_file
    if args.file_partition > 1:
        out_file += '0'
    print("Transform finished, output files refer: {}".format(out_file))
