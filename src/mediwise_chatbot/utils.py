import psycopg2
import json
import re
import pinecone
import time
import os
from openai import OpenAI
from dotenv import load_dotenv
from tenacity import retry, wait_random_exponential, stop_after_attempt
from PyPDF2 import PdfReader
from pinecone import Pinecone, ServerlessSpec, PodSpec


load_dotenv()
client = OpenAI()
pinecone_api_key = os.getenv("PINECONE_API_KEY")
pc = Pinecone(api_key=pinecone_api_key)
embed_model = "text-embedding-3-small"
index_name = 'mediwise-kb'
delimiter = "####"
limit = 8000  #set the limit of knowledge base words, leave some space for chat history and query.


doctors = {
        "dermatologist": ["Calvin Aldrith", "Trudy Ekhart"],
        "otolaryngologist": ["Milford Trinter", "Henry Tallister"],
        "surgeon": ["Travis Redford", "William Kent"],
        "general practitioner": ["Yermol Harrison", "Uncer Patrickson"],
        "radiologist": ["Alfred Renton", "Drew Fanford"],
}

specialties = list(doctors.keys())

availability = {
    "Calvin Aldrith": ["Next Monday at 9am", "Next Wednesday at 2pm"],
    "Trudy Ekhart": ["Next Monday at 9am", "Next Wednesday at 2pm"],
    "Milford Trinter": ["Next Monday at 9am", "Next Wednesday at 2pm"],
    "Henry Tallister": ["Next Monday at 9am", "Next Wednesday at 2pm"],
    "Travis Redford": ["Next Monday at 9am", "Next Wednesday at 2pm"],
    "William Kent": ["Next Monday at 9am", "Next Wednesday at 2pm"],
    "Yermol Harrison": ["Next Monday at 9am", "Next Wednesday at 2pm"],
    "Uncer Patrickson": ["Next Monday at 9am", "Next Wednesday at 2pm"],
    "Alfred Renton": ["Next Monday at 9am", "Next Wednesday at 2pm"],
    "Drew Fanford": ["Next Monday at 9am", "Next Wednesday at 2pm"],
}

availabilities = list(availability.keys())

GPT_MODEL = "gpt-4o"
def chat_complete_messages(messages, temperature):
    completion = client.chat.completions.create(
        model=GPT_MODEL,
        messages= messages,
        temperature=temperature, # this is the degree of randomness of the model's output
    )
    return completion.choices[0].message.content

@retry(wait=wait_random_exponential(multiplier=1, max=40), stop=stop_after_attempt(3))
def chat_completion_request(messages, temperature=0, tools=None, tool_choice=None, model=GPT_MODEL):
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
        return response
    except Exception as e:
        print("Unable to generate ChatCompletion response")
        print(f"Exception: {e}")
        return e

def get_doctors(specialty="general practitioner"):
    """Get the doctors of a certain specialty"""
    return json.dumps(doctors[specialty])

def get_availability(doctor):
    """Get availability of a specific doctor"""
    return json.dumps(availability[doctor])

def get_postgres_conn():
    connection_string = "dbname='medapp' user='postgres' host='0.0.0.0' password='password' port='5432'"
    try:
        conn = psycopg2.connect(connection_string)
        conn.autocommit = True
    except:
        print("I am unable to connect to the database")
    return conn

def get_appointments(patient_id):
    conn = get_postgres_conn() # get postgres conn

    with conn:
        with conn.cursor() as curs:
            try:
                curs.execute("SELECT row_to_json(appointments) FROM appointments where patient_id=%s", [patient_id])
                appointment_rows = curs.fetchall()
                # print(f"{appointment_rows}")
            except (Exception, psycopg2.DatabaseError) as error:
                print(error)

    out = {}
    for app in appointment_rows[0]:          
        out['doctor_id'] = app['doctor_id']
        out['appointment_time'] = app['appointment_start_ts']
    return json.dumps(out)

def table_dml(dml):
    conn = get_postgres_conn()
    error_code = 0
    result = None
    with conn:
        with conn.cursor() as curs:
            try:
                # Assuming you have an active connection and cursor
                curs.execute(dml)

                # Only fetch results for SELECT queries
                if dml.strip().lower().startswith('select'):
                    result = curs.fetchall()
                else:
                    result = None

                # Commit the transaction for DML queries like INSERT, UPDATE, DELETE
                # curs.commit()
                print("SQL commit command completed...")

            except (Exception, psycopg2.Error) as e:
                print("Error while executing DML in PostgreSQL", e)
                error_code = 1
    if error_code != 1 and result == None:
        result = json.dumps({'Changes': 'Successful'})
        return result
    else:
        final_res = {"code": error_code, "res": result}
        return final_res

# A list of functions with descriptions for the LLM to use
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_appointments",
            "description": "Get the current patient appointment details",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {
                        "type": "string",
                        "description": "The patient id",
                    },
                },
                "required": ["patient_id"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_doctors",
            "description": "Get the doctors available based on a specialty",
            "parameters": {
                "type": "object",
                "properties": {
                    "specialty": {
                        "type": "string",
                        "enum": specialties,
                        "description": "The kind of doctor, like a dermatologist",
                    },
                },
                "required": ["specialty"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_availability",
            "description": "Get the availability of a specific doctor",
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor": {
                        "type": "string",
                        "enum": availabilities,
                        "description": "The doctor to check availability with",
                    },
                },
                "required": ["doctor"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "table_dml",
            "description": "Select update insert or delete into appointments table by SQL",
            "parameters": {
                "type": "object",
                "properties": {
                    "dml": {
                        "type": "string",
                        "description": f"""SQL statement to do select update insert and delete on a table, 
                        the SQL should be written using the following database schema:
                        Table name: appointments
                        ####Columns Names and type: 
                            appointment_id SERIAL PRIMARY KEY,
                            doctor_id INTEGER NOT NULL,
                            patient_id  INTEGER NOT NULL,
                            appointment_start_ts timestamp NOT NULL,
                            created_ts timestamp NOT NULL                        
                      ####                        
                        """,
                    },
                    
                },
                "required": ["dml"],
            }
        }
    }, 
]

available_functions = {
            "get_appointments": get_appointments,
            "get_doctors": get_doctors,
            "get_availability": get_availability,
            "table_dml": table_dml,
        }

def tool_call(messages, response_message, tool_calls):

    messages.append(response_message)  # extend conversation with assistant's reply

    if tool_calls:

        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function_to_call = available_functions[function_name]
            function_args = json.loads(tool_call.function.arguments)
            function_response = "you should not be seeing this" # To prevent it from accessing the variable before initialization

            if function_name == 'get_appointments':
                function_response = function_to_call(
                    patient_id=function_args.get("patient_id"),
                    )
            elif function_name == 'get_doctors':
                function_response = function_to_call(
                    specialty=function_args.get("specialty"),
                    )
            elif function_name == 'get_availability':
                function_response = function_to_call(
                    doctor=function_args.get("doctor"),
                    )
            elif function_name == 'table_dml':
                function_response = function_to_call(
                    dml=function_args.get("dml"),
                    )
            print(function_response)
            print(function_response)
            messages.append(
                {
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": function_name,
                    "content": function_response,
                }
            )

            second_response = chat_completion_request(messages, temperature=0, tools=tools, tool_choice="auto")

            if second_response == "Unable to generate ChatCompletion response":
                return "Not found"

            bot_response = second_response.choices[0].message.content

    return bot_response

def index_exists(index_name):
    # Retrieve the list of indexes and store in a variable
    indexes_info = pc.list_indexes()
    
    # Access the 'indexes' key which contains the list of index dictionaries
    for index in indexes_info:
        # Check if the 'name' key in each index dictionary matches the index_name
        if index['name'] == index_name:
            return True
    return False

def create_index(index_name):
    if not index_exists(index_name):
        print(f"Index {index_name} does not exist.")

        pc.create_index(
            name= index_name,
            dimension=1536,
            metric="cosine",
            spec=PodSpec(
                environment="gcp-starter"
                )
            )
        index = pc.Index(index_name)
        # view index stats
        # print(index.describe_index_stats())

def split_text_into_lines(input_text, max_words_per_line):
    words = input_text.split()
    lines = []
    current_line = []

    for word in words:
        if len(current_line) + len(word) + 1 <= max_words_per_line:
            current_line.append(word)
        else:
            lines.append(" ".join(current_line))
            current_line = [word]

    if current_line:
        lines.append(" ".join(current_line))
    return lines


#process the knowledge base file upsert to a namespace
#from tqdm import tqdm

def nlp_upsert(filename, index_name, name_space, nlp_id, chunk_size, stride, page_begin, page_end):
    """
    upsert a whole PDF file (with begin page and end page information) to the pinecone vector database

    Parameters:
    filename (str): The file name.
    index_name (str): The pinecone index name.
    name_space (str): The namespace we want to place for all related docuement.
    nlp_id (str): A common ID prefix to reference to document. 
    chunk_size (int): The chunk size, how many lines as one chunks. 
    stride (int): The overlap side, how many lines as overlap between chunks. 
    page_begin (int): Which page in the PDF file to begin for upsert.
    page_end (int): Which page is the ending page for upsert. 

    Returns:
    None: No return.
    """
    doc = ""
    
    reader = PdfReader(filename)  
    
    for i in range(page_begin, page_end):
        doc += reader.pages[i].extract_text() 
        # print("page completed:", i)    
      

    doc = split_text_into_lines(doc, 30)
    # print("The total lines: ", len(doc))

    
    #Connect to index
    index = pc.Index(index_name)
    
    count = 0
    for i in range(0, len(doc), chunk_size):
        #find begining and end of the chunk
        i_begin = max(0, i-stride)
        i_end = min(len(doc), i_begin+chunk_size)
        
        doc_chunk = doc[i_begin:i_end]
        # print("-"*80)
        # print("The ", i//chunk_size + 1, " doc chunk text:", doc_chunk)
        
        
        texts = ""
        for x in doc_chunk:
            texts += x
        # print("Texts:", texts)
        
        #Create embeddings of the chunk texts
        try:
            res = client.embeddings.create(input=texts, model=embed_model)
        except:
            done = False
            while not done:
                time.sleep(10)
                try:
                    res = client.embeddings.create(input=texts, model=embed_model)
                    done = True
                except:
                    pass
        embed = res.data[0].embedding
        # print("Embeds length:", len(embed))

        # Meta data preparation
        metadata = {
            "text": texts
        }

        count += 1
        # print("Upserted vector count is: ", count)
        # print("="*80)

        #upsert to pinecone and corresponding namespace

        index.upsert(vectors=[{"id": nlp_id + '_' + str(count), "metadata": metadata, "values": embed}], namespace=name_space)


files = ['/data/Fictitious_Doctors_Directory.pdf']
def build_kb(index_name):
    create_index(index_name)
    print(os.getcwd())
    path = os.getcwd()

    for f in files:
        filename = path+f
        print("Knowledge base file name:", filename)
        reader = PdfReader(filename)  
        page_len = len(reader.pages)

        # print("length of the knowledge base file:", page_len)
        nlp_upsert(filename, index_name, "mediwisekb","nlp", 5, 2, 0, page_len)
        index = pc.Index(index_name)
        print(index.describe_index_stats())
    state = 'Reusing KnowledgeBase'
    return state


def get_input_embedding(input):
    # print("input:", input)
    res = client.embeddings.create(
    input=[input],
    model=embed_model
    )
    return res

def retrive_from_pinecone(res, index):
    # retrieve from Pinecone
    xq = res.data[0].embedding

    # get relevant contexts
    res = index.query(vector=xq,
                    top_k=3,
                    include_metadata=True,
                    namespace='mediwisekb')
    contexts = [
        x["metadata"]["text"] for x in res["matches"]
    ]
    return contexts

def build_prompt(contexts):
    prompt = " "
    
    # append contexts until hitting limit
    count = 0
    proceed = True
    while proceed and count < len(contexts):
        if len(prompt) + len(contexts[count]) >= limit:
            proceed = False 
        else:
            prompt += contexts[count]
        
        count += 1
    # End of while loop
    
    prompt = delimiter + prompt + delimiter
    
    return prompt

def build_context_query_knowledge(input, prompt, chatContext):
    input = input + " "
    input_message = {"role": "user", "content": f"""
                    {input}
                    """
                    }
    knowledge_message = {"role": "system", "content": f"""
                        {prompt}
                        """    
                        }
    context_query_knowledge = chatContext + [knowledge_message, input_message]
    # print("context_query_knowledge: ", context_query_knowledge)
    return context_query_knowledge