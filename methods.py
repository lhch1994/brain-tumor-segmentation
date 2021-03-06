from evaluation import dice_scores
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.linear_model import PassiveAggressiveClassifier
from sklearn import svm
from sklearn.metrics import confusion_matrix
from sklearn.externals import joblib
import time
import os
import cPickle as pickle
import matplotlib.pyplot as plt

import patient_plotting as pp
import extras
import data_processing as dp
from experiments import seed

def predict_two_stage(train_pats, test_pats, fscores=None,
                      do_plot_predictions=False, stratified=False, n_trees=30,
                      dev_pats=[], use_mrf=True, resolution=1, n_voxels=30000,
                      mat_dir=None, fresh_models=False, load_hog=False):
    """
    Predict tumor voxels for given test patients.

    Input:
        train_pats -- list of patient IDs used for training a model.
        test_pats -- list of patient IDs used for testing a model.
        fscores -- An opened output file to which we write the results.
    """
    model_str = ""
    if resolution != 1:
        model_str += '_res%d' % resolution
    if load_hog:
        model_str += '_hog'
    model1_fname = os.path.join('models', 'model1_seed%d_ntrp%d_ntep%d_ntrees%d_nvox%s%s.jl' %
                                (seed, len(train_pats), len(test_pats), n_trees, n_voxels, model_str))
    model2_fname = os.path.join('models', 'model2_seed%d_ntrp%d_ntep%d_ntrees%d_nvox%s%s.jl' %
                                (seed, len(train_pats), len(test_pats), n_trees, n_voxels, model_str))

    # Load models if available
    if not fresh_models and os.path.isfile(model1_fname) and \
            os.path.isfile(model2_fname):
        model1 = joblib.load(model1_fname)
        model2 = joblib.load(model2_fname)
        min_voxels = 3000
    else:
        xtr, ytr, coordtr, patient_idxs_tr, dims_tr = dp.load_patients(
                train_pats, stratified, resolution=resolution,
                n_voxels=n_voxels, load_hog=load_hog)

        # Make all tumor labels equal to 1 and train the first model
        ytr1 = np.array(ytr, copy=True)
        ytr1[ytr1>0] = 1
        if stratified:
            # Class frequencies in the whole dataset
            class_counts = [dp.class_counts[0], sum(dp.class_counts[1:])]
            class_freqs = np.asarray(class_counts) / float(sum(class_counts))
            print "Class frequencies (model 1):", class_freqs*100
            # Class frequencies in the sample
            sample_counts = np.histogram(ytr, [0,1,5])[0]
            sample_freqs = sample_counts / float(sum(sample_counts))
            print "Sample frequencies:", sample_freqs*100
            weights = np.ones(len(ytr))
            for i in range(2):
                weights[ytr==i] = class_freqs[i] / sample_freqs[i]
        else:
            weights = None
        model1 = train_RF_model(xtr, ytr1, n_trees=n_trees,
                                sample_weight=weights, fname=model1_fname)

        # Compute minimum number of tumor voxels in a train patient
        min_voxels = 3000#get_min_voxels(ytr, patient_idxs_tr)
        print "Minimum number of voxels in a tumor: %d" % min_voxels

        # Train the second model to separate tumor classes
        ok_idxs = ytr > 0
        xtr2 = np.asarray(xtr[ok_idxs,:])
        ytr2 = np.asarray(ytr[ok_idxs])
        if stratified:
            # Class frequencies in the whole dataset
            class_counts = dp.class_counts[1:]
            class_freqs = np.asarray(class_counts) / float(sum(class_counts))
            print "Class frequencies (model 2):", class_freqs*100
            # Class frequencies in the sample
            sample_counts = np.histogram(ytr, range(1,6))[0]
            sample_freqs = sample_counts / float(sum(sample_counts))
            print "Sample frequencies:", sample_freqs*100
            weights = np.ones(len(ytr2))
            for i in range(4):
                weights[ytr2==i+1] = class_freqs[i] / sample_freqs[i]
        else:
            weights = None
        model2 = train_RF_model(xtr2, ytr2, n_trees=n_trees,
                                sample_weight=weights, fname=model2_fname)

    print "\n----------------------------------\n"

    if len(dev_pats) > 0:
        best_potential = optimize_potential(
                dev_pats, model1, model2, stratified, fscores,
                do_plot_predictions, resolution=resolution, load_hog=load_hog)
        best_radius = optimize_closing(dev_pats, model1, stratified, fscores,
                                       resolution=resolution, load_hog=load_hog)
        best_th = optimize_threshold1(dev_pats, model1, stratified, fscores,
                                      resolution, load_hog, best_radius)
    else:
        best_radius = 6
        best_th = 0.6
        best_potential = np.array([[0.04, 0.03555556, 0.03555556, 0.02222222],
                                   [0.03555556, 0.04, 0.02222222, 0.],
                                   [0.03555556, 0.02222222, 0.04, 0.03555556],
                                   [0.02222222, 0., 0.03555556, 0.04]])


    yte = np.zeros(0)
    predte = np.zeros(0)
    predte_no_pp = np.zeros(0)
    patient_idxs_te = [0]
    print "Test users:"
    # Iterate over test users
    for te_idx, te_pat in enumerate(test_pats):
        print "Test patient number %d" % (te_idx+1)
        x, y, coord, dim = dp.load_patient(te_pat, n_voxels=None,
                                           resolution=resolution,
                                           load_hog=load_hog)

        #pred = model1.predict(x)
        pred_probs = model1.predict_proba(x)
        #pred = np.argmax(pred_probs, axis=1)
        pred = pred_probs[:,1] >= best_th
        # If the predicted tumor is too small set the most probable tumor
        # voxels to one
        if sum(pred > 0) < min_voxels:
            print "Patient having too few voxels (%d < %d)" % (sum(pred > 0), min_voxels)
            pred = np.zeros(pred.shape)
            new_idxs = np.argsort(pred_probs[:,1])[-min_voxels:]
            pred[new_idxs] = 1
        pp_pred = dp.post_process(coord, dim, pred, binary_closing=True,
                                  radius=best_radius)

        tumor_idxs = pp_pred > 0
        if sum(tumor_idxs) > 0:
            pred_probs2 = model2.predict_proba(x[tumor_idxs,:])
            pred2 = np.argmax(pred_probs2, axis=1) + 1
            pp_pred[tumor_idxs] = pred2

        pp_pred15 = np.array(pp_pred)
        print "\nConfusion matrix:"
        cm = confusion_matrix(y, pp_pred15)
        print cm
        dice_scores(y, pp_pred15, label='Dice scores:')

        if use_mrf:
            # MRF post processing
            if sum(tumor_idxs) > 0:
                edges = dp.create_graph(coord[tumor_idxs,:])
                pp_pred[tumor_idxs] = dp.mrf(pred_probs2, edges,
                                             potential=best_potential) + 1
            method = 'MRF'
        else:
            # Closing post processing
            if sum(tumor_idxs) > 0:
                pp_pred[tumor_idxs] = dp.post_process(coord[tumor_idxs,:], dim,
                                                      pred2, remove_components=False,
                                                      radius=best_radius)
            method = 'closing'

        print "\nConfusion matrix (pp):"
        cm = confusion_matrix(y, pp_pred)
        print cm

        yte = np.concatenate((yte, y))
        patient_idxs_te.append(len(yte))
        predte = np.concatenate((predte, pp_pred))
        predte_no_pp = np.concatenate((predte_no_pp, pp_pred15))

        dice_scores(y, pp_pred, label='Dice scores (pp):')

        if do_plot_predictions:
            # Plot the patient
            pif = os.path.join('results', 'pat%d_slices_2S_%s.png' % (te_pat, method))
            if mat_dir is not None:
                fmat = os.path.join(mat_dir, 'pat%d.mat' % te_pat)
            else:
                fmat = None
            pp.plot_predictions(coord, dim, pp_pred15, y, pp_pred, fname=pif,
                                fmat=fmat)
            #if pred_fname is not None:
            #    extras.save_predictions(coord, dim_list[0], pred, yte, pred_fname)

    print "\nOverall confusion matrix:"
    cm = confusion_matrix(yte, predte)
    print cm

    dice_scores(yte, predte_no_pp, patient_idxs=patient_idxs_te,
                label='Overall dice scores (two-stage, no pp):', fscores=fscores)

    dice_scores(yte, predte, patient_idxs=patient_idxs_te,
                label='Overall dice scores (two-stage):', fscores=fscores)

def train_RF_model(xtr, ytr, n_trees=30, sample_weight=None, fname=None):
    # Train classifier
    t0 = time.time()
    model = RandomForestClassifier(n_trees, oob_score=True, verbose=1,
                                   n_jobs=16)#, class_weight='auto')
    #model = ExtraTreesClassifier(n_trees, verbose=1, n_jobs=4)
    #model = svm.SVC(C=1000)
    model.fit(xtr, ytr, sample_weight=sample_weight)
    if fname is not None:
        joblib.dump(model, fname)
    print "Training/loading took %.2f seconds" % (time.time()-t0)
    #print "OOB score: %.2f%%" % (model.oob_score_*100)
    '''
    print "Feature importances:"
    for i in range(4):
        print model.feature_importances_[i*20:(i+1)*20]
    '''
    best_feats = np.argsort(model.feature_importances_)[::-1]
    print best_feats
    print model.feature_importances_[best_feats]
    return model

def optimize_closing(dev_pats, model1, stratified, fscores=None, resolution=1,
                     load_hog=False):
    radii = [1,2,3,4,5,6,7,8,9,10]
    nr = len(radii)

    yde = np.zeros(0)
    predde = np.zeros((0, nr))
    predde_no_pp = np.zeros(0)
    patient_idxs_de = [0]
    print "Development users:"
    # Iterate over dev users
    for de_idx, de_pat in enumerate(dev_pats):
        print "Development patient number %d" % (de_idx+1)
        x, y, coord, dim = dp.load_patient(de_pat, n_voxels=None,
                                           resolution=resolution,
                                           load_hog=load_hog)
        yde = np.concatenate((yde, y))
        patient_idxs_de.append(len(yde))

        pred = model1.predict(x)
        dice_scores(y, pred, label='Dice scores (dev, no closing):')

        predde_no_pp = np.concatenate((predde_no_pp, pred))
        predde_part = dp.post_process_multi_radii(
                coord, dim, pred, radii, y, remove_components=True,
                binary_closing=True)
        predde = np.vstack((predde, predde_part))

    dice_scores(yde, predde_no_pp, patient_idxs=patient_idxs_de,
                label='Overall dice scores (two-stage, no MRF):', fscores=fscores)

    best_r = radii[0]
    best_score = -1
    for i in range(nr):
        print "\nOverall confusion matrix (r=%d):" % radii[i]
        cm = confusion_matrix(yde, predde[:,i])
        print cm

        ds = dice_scores(yde, predde[:,i], patient_idxs=patient_idxs_de,
                         label='Overall dice scores (two-stage, r=%d):' % radii[i],
                         fscores=fscores)
        score = sum(ds)
        if score > best_score:
            best_score = score
            best_r = radii[i]
    print "Best r=%d, score=%f:" % (best_r, best_score)
    return best_r

def optimize_threshold1(dev_pats, model1, stratified, fscores=None, resolution=1,
                        load_hog=False, best_radius=3):
    #ths = [0.25, 0.35, 0.4, 0.45, 0.5, 0.6]
    ths = [0.55, 0.57, 0.58, 0.59, 0.6, 0.61, 0.62, 0.63, 0.65]
    nt = len(ths)

    yde = np.zeros(0)
    preds = []
    for i in range(nt):
        preds.append(np.zeros(0))
    patient_idxs_de = [0]
    print "Development users:"
    # Iterate over dev users
    for de_idx, de_pat in enumerate(dev_pats):
        print "Development patient number %d" % (de_idx+1)
        x, y, coord, dim = dp.load_patient(de_pat, n_voxels=None,
                                           resolution=resolution,
                                           load_hog=load_hog)
        yde = np.concatenate((yde, y))
        patient_idxs_de.append(len(yde))

        pred_probs = model1.predict_proba(x)
        for i in range(nt):
            threshold = ths[i]
            pred = pred_probs[:,1] >= threshold
            pp_pred = dp.post_process(coord, dim, pred, binary_closing=True,
                                      radius=best_radius)
            preds[i] = np.concatenate((preds[i], pp_pred))
            dice_scores(y, pp_pred, patient_idxs=None,
                        label='Dice scores (two-stage, th=%.2f):' % ths[i])

    best_th = ths[0]
    best_score = -1
    for i in range(nt):
        print "\nOverall confusion matrix (th=%.2f):" % ths[i]
        cm = confusion_matrix(yde, preds[i])
        print cm

        ds = dice_scores(yde, preds[i], patient_idxs=patient_idxs_de,
                         label='Overall dice scores (two-stage, th=%.2f):' % ths[i],
                         fscores=fscores)
        score = sum(ds)
        if score > best_score:
            best_score = score
            best_th = ths[i]
    print "Best th=%.2f, score=%f:" % (best_th, best_score)
    return best_th

def optimize_potential(dev_pats, model1, model2, stratified, fscores=None,
                       do_plot_predictions=False, resolution=1, load_hog=False):
    n_labels = 4
    potentials = []
    factors = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1]
    #factors = [0.00001, 0.0001, 0.001, 0.01, 0.02, 0.05, 0.1]
    # Quadratic potential
    order = [2,1,3,4]
    pot_mat = np.zeros((n_labels, n_labels))
    for i in range(len(order)):
        for j in range(len(order)):
            pot_mat[i,j] = np.abs(order[i] - order[j])**2
    max_val = np.max(pot_mat[:])
    pot_mat = (max_val - pot_mat) / max_val
    for f in factors:
        #potentials.append(f * np.eye(n_labels))
        potentials.append(f * pot_mat)
    n_pots = len(potentials)

    yde = np.zeros(0)
    predde = np.zeros((0, n_pots))
    predde_no_pp = np.zeros(0)
    patient_idxs_de = [0]
    print "Development users:"
    # Iterate over dev users
    for de_idx, de_pat in enumerate(dev_pats):
        print "Development patient number %d" % (de_idx+1)
        x, y, coord, dim = dp.load_patient(de_pat, n_voxels=None,
                                           resolution=resolution,
                                           load_hog=load_hog)
        yde = np.concatenate((yde, y))
        patient_idxs_de.append(len(yde))

        pred = model1.predict(x)
        pp_pred = dp.post_process(coord, dim, pred, binary_closing=True)

        tumor_idxs = pp_pred > 0
        pred_probs2 = model2.predict_proba(x[tumor_idxs,:])
        pred2 = np.argmax(pred_probs2, axis=1) + 1
        pp_pred[tumor_idxs] = pred2

        pp_pred15 = np.array(pp_pred)
        print "\nConfusion matrix (dev):"
        cm = confusion_matrix(y, pp_pred15)
        print cm
        dice_scores(y, pp_pred15, label='Dice scores (dev, no MRF):')
        predde_no_pp = np.concatenate((predde_no_pp, pp_pred15))
        predde_part = np.zeros((len(pp_pred15), 0))

        edges = dp.create_graph(coord[tumor_idxs,:])
        for pi, pot in enumerate(potentials):
            print "  Patient %d, potential %d." % (de_idx+1, pi+1)
            pp_pred[tumor_idxs] = dp.mrf(pred_probs2, edges, potential=pot) + 1

            print "\nConfusion matrix (MRF-%d):" % (pi+1)
            cm = confusion_matrix(y, pp_pred)
            print cm

            predde_part = np.hstack((predde_part, pp_pred.reshape(len(pp_pred),1)))

            dice_scores(y, pp_pred, label='Dice scores (pp):')

            if do_plot_predictions or de_idx < 5:
                # Plot the patient
                pif = os.path.join('plots', 'validation2', 'pat%d_slices_2S_MRF-%d.png' % (de_pat,pi+1))
                pp.plot_predictions(coord, dim, pp_pred15, y, pp_pred, fname=pif)
                #if pred_fname is not None:
                #    extras.save_predictions(coord, dim_list[0], pred, yte, pred_fname)
        predde = np.vstack((predde, predde_part))

    dice_scores(yde, predde_no_pp, patient_idxs=patient_idxs_de,
                label='Overall dice scores (two-stage, no MRF):', fscores=fscores)

    best_potential = potentials[0]
    best_score = -1
    for i in range(n_pots):
        print "\nOverall confusion matrix (%d):" % i
        cm = confusion_matrix(yde, predde[:,i])
        print cm

        ds = dice_scores(yde, predde[:,i], patient_idxs=patient_idxs_de,
                         label='Overall dice scores (two-stage, MRF-%d):' % i,
                         fscores=fscores)
        score = sum(ds)
        if score > best_score:
            best_score = score
            best_potential = potentials[i]
    print "Best potential (score=%f):" % (best_score)
    print best_potential
    return best_potential

def get_min_voxels(y, patient_idxs):
    min_size = 1e9
    for i in range(len(patient_idxs)-1):
        yy = y[patient_idxs[i]:patient_idxs[i+1]]
        tumor_size = sum(yy > 0)
        if tumor_size < min_size:
            min_size = tumor_size
    return min_size

def predict_RF(train_pats, test_pats, fscores=None, do_plot_predictions=False,
               stratified=False):
    """
    Predict tumor voxels for given test patients.

    Input:
        train_pats -- list of patient IDs used for training a model.
        test_pats -- list of patient IDs used for testing a model.
        fscores -- An opened output file to which we write the results.
    """
    xtr, ytr, coordtr, patient_idxs_tr, dims_tr = dp.load_patients(train_pats,
                                                                   stratified)

    if stratified:
        # Class frequencies in the whole dataset
        class_freqs = dp.class_counts / float(sum(dp.class_counts))
        print "Class frequencies:", class_freqs*100
        # Class frequencies in the sample
        sample_counts = np.histogram(ytr, range(6))[0]
        sample_freqs = sample_counts / float(sum(sample_counts))
        print "Sample frequencies:", sample_freqs*100
        weights = np.ones(len(ytr))
        for i in range(5):
            weights[ytr==i] = class_freqs[i] / sample_freqs[i]
    else:
        weights = None
    model = train_RF_model(xtr, ytr, n_trees=30, sample_weight=weights)

    print "\n----------------------------------\n"

    yte = np.zeros(0)
    predte = np.zeros(0)
    patient_idxs_te = [0]
    print "Test users:"
    # Iterate over test users
    for te_idx, te_pat in enumerate(test_pats):
        print "Test patient number %d" % (te_idx+1)
        x, y, coord, dim = dp.load_patient(te_pat, n_voxels=None)

        if do_plot_predictions:
            pif = os.path.join('plots', 'pat%d_slices_0_RF.png' % te_pat)
        else:
            pif = None
        pred = predict_and_evaluate(
                model, x, y, coord=coord, dim_list=[dim], plot_confmat=False,
                ret_probs=False, patient_idxs=None,
                pred_img_fname=pif)
        #pred = np.argmax(pred_probs_te, axis=1)

        yte = np.concatenate((yte, y))
        patient_idxs_te.append(len(yte))
        predte = np.concatenate((predte, pred))
        '''
        for i in range(1):
            xlabel_te = dp.extract_label_features(coordte, dims_te, pred_probs_te,
                                                  patient_idxs_te)
            smoothed_pred = np.argmax(xlabel_te, axis=1)
            dice_scores(yte, smoothed_pred, patient_idxs=patient_idxs_te,
                        label='Test smoothed dice scores (iteration %d):' % (i+1))
    
            xte2 = np.hstack((xte, xlabel_te))
            pred_probs_te = predict_and_evaluate(
                    model2, xte2, yte, coord=coordte, dim_list=dims_te, pred_fname=None,
                    plot_confmat=False, ret_probs=True, patient_idxs=patient_idxs_te,
                    pred_img_fname=os.path.join('plots', 'pat%d_slices_%d.png' % (test_pats[0], i+1)))
        '''

    print "\nOverall confusion matrix:"
    cm = confusion_matrix(yte, predte)
    print cm

    dice_scores(yte, predte, patient_idxs=patient_idxs_te,
                label='Overall dice scores (RF):', fscores=fscores)

def predict_online(train_pats, test_pats, fscores=None, do_plot_predictions=False,
                   stratified=False):
    """
    Predict tumor voxels for given test patients.

    Input:
        train_pats -- list of patient IDs used for training a model.
        test_pats -- list of patient IDs used for testing a model.
        fscores -- An opened output file to which we write the results.
    """

    #xtr, ytr, coordtr, patient_idxs_tr, dims_tr = dp.load_patients(train_pats,
    #                                                               stratified)
    model = None
    for tr_idx, tr_pat in enumerate(train_pats):
        print "Train patient number %d" % (tr_idx+1)
        x, y, coord, dim = dp.load_patient(tr_pat, n_voxels=10000)
        model = train_online_model(x, y, model)

    print "\n----------------------------------\n"

    yte = np.zeros(0)
    predte = np.zeros(0)
    patient_idxs_te = [0]
    print "Test users:"
    # Iterate over test users
    for te_idx, te_pat in enumerate(test_pats):
        print "Test patient number %d" % (te_idx+1)
        x, y, coord, dim = dp.load_patient(te_pat, n_voxels=None)

        if do_plot_predictions:
            pif = os.path.join('plots', 'pat%d_slices_0_online.png' % te_pat)
        else:
            pif = None
        pred = predict_and_evaluate(
                model, x, y, coord=coord, dim_list=[dim], plot_confmat=False,
                ret_probs=False, patient_idxs=None,
                pred_img_fname=pif)

        yte = np.concatenate((yte, y))
        patient_idxs_te.append(len(yte))
        predte = np.concatenate((predte, pred))

    print "\nOverall confusion matrix:"
    cm = confusion_matrix(yte, predte)
    print cm

    dice_scores(yte, predte, patient_idxs=patient_idxs_te,
                label='Overall dice scores (RF):', fscores=fscores)

def train_online_model(xtr, ytr, model=None):
    # Train classifier
    t0 = time.time()
    if model is None:
        model = PassiveAggressiveClassifier()
        model.fit(xtr, ytr)
    else:
        model.partial_fit(xtr, ytr)
    print "Training took %.2f seconds" % (time.time()-t0)
    return model

def predict_and_evaluate(model, xte, yte=None, coord=None, dim_list=None,
                         pred_fname=None, plot_confmat=False, ret_probs=False,
                         patient_idxs=None, pred_img_fname=None,
                         pred_3D_fname=None):

    # Predict and evaluate
    pred_probs = model.predict_proba(xte)
    print "Pred probs size:", pred_probs.shape
    pred = np.argmax(pred_probs, axis=1)
    #pp.save_pred_probs_csv(coord, dim_list[0], pred_probs, 'pred_probs.csv')

    if yte is not None:
        print "\nConfusion matrix:"
        cm = confusion_matrix(yte, pred)
        print cm

        acc = sum(pred==yte) / float(len(pred))
        bl_acc = sum(yte==0) / float(len(pred))
        print "Accuracy:\t%.2f%%" % (acc*100)
        print "Majority vote:\t%.2f%%" % (bl_acc*100)

        dice_scores(yte, pred, patient_idxs=patient_idxs)
 
        pp_pred = dp.post_process(coord, dim_list[0], pred, pred_probs)
        dice_scores(yte, pp_pred, patient_idxs=patient_idxs, label='Dice scores (pp):')

        if coord is not None and dim_list is not None and pred_img_fname is not None:
            # Plot the first patient
            if patient_idxs is None:
                patient_idxs = [0, len(yte)]
            pp.plot_predictions(coord[:patient_idxs[1]], dim_list[0], 
                                pred[:patient_idxs[1]], yte[:patient_idxs[1]],
                                pp_pred=pp_pred[:patient_idxs[1]], fname=pred_img_fname,
                                fpickle=pred_3D_fname)
        if pred_fname is not None:
            extras.save_predictions(coord, dim_list[0], pred, yte, pred_fname)

        if plot_confmat:
            plt.figure()
            pp.plot_confusion_matrix(cm)
            plt.show()

    if ret_probs:
        return pred_probs
    else:
        return pp_pred
