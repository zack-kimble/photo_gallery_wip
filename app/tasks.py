import os
import sys
import warnings
import traceback
from time import perf_counter

import torch
from PIL import Image
from facenet_pytorch import MTCNN, training
from facenet_pytorch.models.utils.detect_face import save_img
from rq import get_current_job
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms
from torchvision.datasets.folder import pil_loader

from app import create_app, Config
from app import db
from app.models import Photo, PhotoFace, Task, FaceEmbedding

import numpy as np

from sklearn.neighbors import RadiusNeighborsClassifier

from time import sleep

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite://'
    ELASTICSEARCH_URL = None
    UPLOAD_FOLDER = 'test_assets/uploads'

if os.environ.get('PYTEST'):
    app = create_app(TestConfig)
    app.app_context().push()
else:
    app = create_app()
    app.app_context().push()

print("after context push:"+os.getcwd())
#def run_search

# Making dataset return image and path instead of image and label. Probably better to just stop using Dataset and Dataloader at some point, but whatever
class ImagePathsDataset(Dataset):
    def __init__(self, data, loader=pil_loader, transform=lambda x: x):
        self.data = data
        self.loader = loader
        self.transform = transform

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        orig_path = self.data[idx]
        img = self.loader(orig_path)
        if self.transform is not None:
            img = self.transform(img)
        return img, orig_path


def exif_rotate_pil_loader(path):
    with open(path, 'rb') as f:
        image = Image.open(f)
        image = reorient_image(image)
        image = image.convert('RGB')  # replicates pil_loader from torchvision. Copies to convert to PIL
    return image


def reorient_image(im):
    try:
        image_exif = im._getexif()
        image_orientation = image_exif[274]
        if image_orientation in (2, '2'):
            return im.transpose(Image.FLIP_LEFT_RIGHT)
        elif image_orientation in (3, '3'):
            return im.transpose(Image.ROTATE_180)
        elif image_orientation in (4, '4'):
            return im.transpose(Image.FLIP_TOP_BOTTOM)
        elif image_orientation in (5, '5'):
            return im.transpose(Image.ROTATE_90).transpose(Image.FLIP_TOP_BOTTOM)
        elif image_orientation in (6, '6'):
            return im.transpose(Image.ROTATE_270)
        elif image_orientation in (7, '7'):
            return im.transpose(Image.ROTATE_270).transpose(Image.FLIP_TOP_BOTTOM)
        elif image_orientation in (8, '8'):
            return im.transpose(Image.ROTATE_90)
        else:
            return im
    except (KeyError, AttributeError, TypeError, IndexError):
        return im

def recommend_batch_size(overhead_mb, obs_mb, safety_margin):
    total_mem_mb = torch.cuda.get_device_properties(0).total_memory/1e6
    available_mem = total_mem_mb - overhead_mb
    max_batch_size = available_mem//obs_mb
    recommended_size = int(np.floor(max_batch_size * (1-safety_margin)))
    return recommended_size

def init_mtcnn():
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    app.logger.info('Running on device: {}'.format(device))

    mtcnn = MTCNN(
        image_size=112,
        margin=10,
        min_face_size=20,
        thresholds=[0.6, 0.7, 0.7],
        factor=0.709,
        post_process=False,
        keep_all=True,
        device=device
    )
    return mtcnn


def mtcnn_detect_faces(images, mtcnn, batch_size):

    workers = 0 if os.name == 'nt' else os.cpu_count()
    app.logger.info('Number of workers {}'.format(workers))

    img_ds = ImagePathsDataset(images, loader=exif_rotate_pil_loader, transform=transforms.Resize((1024, 1024)))

    loader = DataLoader(
        img_ds,
        num_workers=workers,
        batch_size=batch_size,
        collate_fn=training.collate_pil
    )

    paths = []
    boxes = []
    box_probs = []
    faces = []

    for i, (x, b_paths) in enumerate(loader):
        # save_paths = [photo_path.replace('test_assets/uploads', storage_root) for photo_path in b_paths]
        # face_path = [p.replace(os.path.basename(p), os.path.basename(p) + '_face') for p in b_paths]
        b_boxes, b_box_probs, points = mtcnn.detect(x, landmarks=True)

        b_faces = mtcnn.extract(x, b_boxes, save_path=None)

        boxes.extend(b_boxes)
        box_probs.extend(b_box_probs)
        paths.extend(b_paths)
        faces.extend(b_faces)

    return paths, boxes, box_probs, faces


def get_arcface_embeddings(images):
    app.logger.info("arc face func: "+os.getcwd())
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    app.logger.info('Running on device: {}'.format(device))
    batch_size = 16
    workers = 0 if os.name == 'nt' else os.cpu_count()
    app.logger.info('Number of workers {}'.format(workers))

    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ])

    img_ds = ImagePathsDataset(images, transform=transform)
    loader = DataLoader(img_ds, num_workers=workers,
                        batch_size=batch_size)

    from app.insightface import model_loader #TODO figure out how to change where torch looks for model module when loading. Currently looks in the task cwd, which is the project root.

    model = model_loader(device)
    model = model.to(device)
    model.eval()

    embeddings = []
    img_paths = []

    with torch.no_grad():
        for xb, img_path in loader:
            xb = xb.to(device)
            b_embeddings = model(xb)
            b_embeddings = b_embeddings.to('cpu').numpy()

            embeddings.extend(b_embeddings)
            img_paths.extend(img_path)

    return embeddings


def store_face(face, save_path):
    os.makedirs(os.path.dirname(save_path) + "/", exist_ok=True)
    save_img(face, save_path)


def detect_faces_task(storage_root, outer_batch_size='auto'):
    # TODO Not sure about the try/except wrapping here.
    app.logger.info(f"saving photos in {storage_root}")
    try:
        # get image data from db
        photos_result = Photo.query.filter_by(face_detection_run=False).filter(~Photo.photo_faces.any()).all()

        if (new_photo_count := len(photos_result)) == 0:
            app.logger.warn('no new photos found to run detection on', exc_info = sys.exc_info())
            job_meta = {'progress': 100, 'warning': 'no new photos found to run detect on'}
            update_task(job_meta)
            return

        app.logger.info(f'detecting faces in {new_photo_count} photos')

        mtcnn = init_mtcnn()
        if torch.cuda.is_available():
            rec_batch_size = recommend_batch_size(850, 72, .2) # based on observed model and image tensor gpu memory utilization
        else:
            rec_batch_size = 16
        outer_batch_size = rec_batch_size*10 if outer_batch_size == 'auto' else outer_batch_size
        total_batches = int(np.ceil(new_photo_count / outer_batch_size))
        batch_times = []
        for b in range(total_batches):
            start = perf_counter()
            app.logger.info(f'starting batch {b}')
            start_b = b * outer_batch_size
            end_b = min((start_b + outer_batch_size, new_photo_count))
            photos_result_batch = photos_result[start_b:end_b]
            photo_id_list, photo_paths_list = list(zip(*[(photo.id, photo.location) for photo in photos_result_batch]))

            # get paths figured out - doing this here instead of in the mtcnn wrapper. Should be fine since the dataloader is sequential
            load_paths_list = [os.path.join(app.config['UPLOAD_FOLDER'], photo_path) for photo_path in photo_paths_list]
            # save_paths_list = [os.path.join(storage_root, photo_path) for photo_path in photo_paths_list]

            paths_list, boxes_list, box_probs_list, faces_list = mtcnn_detect_faces(load_paths_list, mtcnn, rec_batch_size)
            face_meta_list = []

            for photo_id, path, boxes, probs, faces in zip(photo_id_list, photo_paths_list, boxes_list, box_probs_list,
                                                           faces_list):
                image_path, ext = os.path.splitext(path)
                if faces is not None:
                    faces = list(transforms.functional.to_pil_image(x * 255) for x in faces.unbind())
                    for i, (box, prob, face) in enumerate(zip(boxes, probs, faces)):
                        save_path = storage_root + '/' + image_path + '_' + str(i) + ext
                        db_face = PhotoFace(location=save_path,
                                            sequence=i,
                                            bb_x1=box[0],
                                            bb_y1=box[1],
                                            bb_x2=box[2],
                                            bb_y2=box[3],
                                            bb_prob=prob,
                                            photo_id=photo_id,
                                            bb_auto=True
                                            )
                        db.session.add(db_face)
                        store_face(face, save_path)
                Photo.query.get(photo_id).face_detection_run = True
            db.session.commit()
            completed_photo_count = min((b+1)*outer_batch_size,new_photo_count)
            end = perf_counter()
            batch_times.append(end-start)
            mean_batch_time_min = np.mean(batch_times) / 60
            app.logger.info(f"completed detection on {completed_photo_count} photos")
            job_meta = {'photos_to_be_processed': new_photo_count,
                        'batch_size': outer_batch_size,
                        'total_batches': total_batches,
                        'batches_complete': b+1,
                        'progress': 100*(completed_photo_count/new_photo_count),
                        'mean_batch_time_minutes': mean_batch_time_min,
                        'eta_minutes': mean_batch_time_min*(total_batches-b+1),
                        'elapsed_minutes': sum(batch_times)/60,
                        }
            update_task(job_meta)
        del mtcnn
    except:
        app.logger.error('Unhandled exception', exc_info=sys.exc_info())
        fail_task()
        traceback.print_exc()
        raise ChildProcessError(sys.exc_info())




def fail_task():
    job_meta = {'task_failed': True, 'progress': 100}
    db.session.rollback()
    update_task(job_meta)

def update_task(job_meta):
    job = get_current_job()
    if job:
        job.meta.update(job_meta)
        # for k, v in job_meta.items():
        #     job.meta[k] = v
        job.save_meta()
        task = Task.query.get(job.get_id())
        task.meta = job.meta
        task.progress = job.meta['progress']
        if task.progress >= 100:
            task.complete = True
        db.session.commit()

def create_embeddings_task(outer_batch_size=480):
    app.logger.info("embeddings task: "+os.getcwd())
    try:
        faces_result = PhotoFace.query.filter(~PhotoFace.embedding.any()).all()
        if (new_face_count := len(faces_result)) == 0:
            app.logger.warn('no new faces found to run embedding on', exc_info = sys.exc_info())
            job_meta = {'progress': 100, 'warning': 'no new faces found to run embedding on'}
            update_task(job_meta)
            return

        total_batches = int(np.ceil(new_face_count/outer_batch_size))
        batch_times = []
        for b in range(total_batches):
            start = perf_counter()
            start_b = b * outer_batch_size
            end_b = min((start_b + outer_batch_size, new_face_count))
            faces_result_batch = faces_result[start_b:end_b]
            face_id_list, face_paths_list = list(zip(*[(face.id, face.location) for face in faces_result_batch]))
            face_embeddings = get_arcface_embeddings(face_paths_list)
            for id, embedding in zip(face_id_list, face_embeddings):
                face_embedding = FaceEmbedding(embedding=embedding, photo_face_id=id)
                db.session.add(face_embedding)
            db.session.commit()
            end = perf_counter()
            batch_times.append(end-start)
            mean_batch_time_min = np.mean(batch_times) / 60
            completed_face_count =  min((b + 1) * outer_batch_size, new_face_count)
            app.logger.info(f"completed detection on {completed_face_count} faces")
            job_meta = {'faces_to_be_processed': new_face_count,
                        'batch_size': outer_batch_size,
                        'total_batches': total_batches,
                        'batches_complete': b+1,
                        'progress': 100*(completed_face_count/new_face_count),
                        'mean_batch_time_minutes': mean_batch_time_min,
                        'eta_minutes': mean_batch_time_min*(total_batches-b+1),
                        'elapsed_minutes': sum(batch_times)/60,
                        }
            update_task(job_meta)
    except:
        app.logger.error('Unhandled exception', exc_info=sys.exc_info())
        fail_task()
        traceback.print_exc()
        raise ChildProcessError(sys.exc_info())



def angular_distance(feature0, feature1):
    x0 = feature0 / np.linalg.norm(feature0)
    x1 = feature1 / np.linalg.norm(feature1)
    cosine = np.dot(x0,x1)
    cosine = np.clip(cosine, -1.0, 1.0)
    theta = np.arccos(cosine)
    theta = theta * 180 / np.pi
    return theta


def identify_faces_task():
    try:
        gallery = PhotoFace.query.filter(PhotoFace.name_auto.is_(False)& PhotoFace.embedding.any()).all()
        if len(gallery) == 0:
            raise ValueError('no faces have been labeled, so no new faces can be identified')
        X, y = zip(*[(photo_face.embedding[0].embedding, photo_face.name) for photo_face in gallery])
        #TODO convert names to ints and map then map back after
        y = np.array(y).astype('<U20') #hack because RadiusNeighborsClassifier converts y levels to np.array and then compares dtypes. If the outlier label is longer than any name it will fail.
        radius_knn = RadiusNeighborsClassifier(radius=73, metric=angular_distance, outlier_label='Unknown', weights='distance')
        radius_knn.fit(X, y)
        
        # will redo any previously autolabeled faces
        probes = PhotoFace.query.filter(PhotoFace.name_auto.isnot(False) & PhotoFace.embedding.any()).all() 
        X_test, probe_ids = zip(*[(photo_face.embedding[0].embedding, photo_face.id) for photo_face in probes])
        predict_names = radius_knn.predict(X_test)
        for id, name in zip(probe_ids, predict_names):
            photo_face = PhotoFace.query.get(id)
            photo_face.from_dict({'name': name, 'name_auto': True})
        db.session.commit()
        job_meta = {'progress': 100}
        update_task(job_meta)
        return
    except:
        app.logger.error('Unhandled exception', exc_info=sys.exc_info())
        fail_task()
        traceback.print_exc()
        raise ChildProcessError(sys.exc_info())

def test_task():
    sleep(10)
    app.logger.info('task done')
    fail_task()
    return




